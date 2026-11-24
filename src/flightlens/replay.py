"""The replay artifact: one flight resampled to a uniform clock for playback.

``build_replay(flight)`` turns the common signals into one table the browser
can play back cheaply (see ``docs/replay.md`` for what the user sees):

- **Position** in metres east, north and up from the take-off point, with the
  same equirectangular projection as the web UI's map (``TrackView``), so 2D
  and 3D agree. Height is the logged height above take-off, never AMSL, so the
  aircraft cannot appear underground because of GPS or barometer offsets.
- **Attitude** as a unit quaternion (body to north-east-down, Hamilton
  ``w, x, y, z``): the logged one when present (PX4 ``att.q0..q3``), otherwise
  built from roll, pitch and yaw. It is interpolated by slerp, so there are no
  flips near 90° pitch or at the 359° → 0° heading wrap.
- **Gaps** of more than ``MAX_GAP_S`` between samples are never bridged: the
  values there are NaN and ``valid_pos`` / ``valid_att`` are 0.

The grid is ``ONBOARD_RATE_HZ`` for onboard logs, and the log's own attitude
rate (at most ``TLOG_MAX_RATE_HZ``) for telemetry logs, which are slow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from flightlens.checks._util import hold, resample
from flightlens.model import EventKind, Flight, Signal

REPLAY_VERSION = "1.0.0"
"""Bump when the table's columns or method change: older replays become outdated."""
ONBOARD_RATE_HZ = 25.0
TLOG_MAX_RATE_HZ = 10.0
MAX_GAP_S = 1.0
EARTH_RADIUS_M = 6_371_000.0
"""Same radius as the web UI's map, so both project positions identically."""
MAX_MOTORS = 8

FloatArray = NDArray[np.float64]


# --- frame and attitude ----------------------------------------------------------------


def local_frame(
    lat: FloatArray, lon: FloatArray, lat0: float, lon0: float
) -> tuple[FloatArray, FloatArray]:
    """Metres east and north of (lat0, lon0): equirectangular, as ``TrackView`` draws."""
    k = np.pi / 180
    east = (np.asarray(lon) - lon0) * k * EARTH_RADIUS_M * np.cos(lat0 * k)
    north = (np.asarray(lat) - lat0) * k * EARTH_RADIUS_M
    return east, north


def euler_to_quat(roll: FloatArray, pitch: FloatArray, yaw: FloatArray) -> FloatArray:
    """Roll, pitch, yaw (degrees, aerospace Z-Y-X) to quaternions, shape (n, 4) as w, x, y, z."""
    r, p, y = (np.radians(np.asarray(a, dtype=np.float64)) / 2 for a in (roll, pitch, yaw))
    cr, sr, cp, sp, cy, sy = np.cos(r), np.sin(r), np.cos(p), np.sin(p), np.cos(y), np.sin(y)
    q = np.stack(
        [
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        ],
        axis=-1,
    )
    return np.asarray(q, dtype=np.float64)


def quat_to_euler(q: FloatArray) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Quaternions (n, 4) to roll, pitch, yaw in degrees (yaw in -180..180)."""
    w, x, y, z = (q[..., i] for i in range(4))
    roll = np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = np.arcsin(np.clip(2 * (w * y - z * x), -1.0, 1.0))
    yaw = np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return np.degrees(roll), np.degrees(pitch), np.degrees(yaw)


def normalize(q: FloatArray) -> FloatArray:
    n = np.linalg.norm(q, axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.asarray(q / n, dtype=np.float64)


def slerp(q0: FloatArray, q1: FloatArray, f: FloatArray) -> FloatArray:
    """Spherical interpolation between unit quaternions (n, 4) by fractions f (n,),
    along the shorter arc."""
    q0, q1 = normalize(q0), normalize(q1)
    dot = np.sum(q0 * q1, axis=-1)
    q1 = np.where(dot[:, None] < 0, -q1, q1)
    dot = np.abs(dot)
    f = np.asarray(f, dtype=np.float64)[:, None]
    theta = np.arccos(np.clip(dot, -1.0, 1.0))[:, None]
    sin = np.sin(theta)
    near = sin[:, 0] < 1e-6  # almost the same attitude: linear is exact enough
    with np.errstate(invalid="ignore", divide="ignore"):
        a = np.where(near[:, None], 1 - f, np.sin((1 - f) * theta) / sin)
        b = np.where(near[:, None], f, np.sin(f * theta) / sin)
    return normalize(a * q0 + b * q1)


def unwrap_yaw(yaw_deg: FloatArray) -> FloatArray:
    """Heading made continuous (359° → 361°, not → 1°), NaNs kept in place."""
    out = np.asarray(yaw_deg, dtype=np.float64).copy()
    ok = np.isfinite(out)
    out[ok] = np.degrees(np.unwrap(np.radians(out[ok])))
    return out


def _gap_mask(st: FloatArray, grid: FloatArray, max_gap: float) -> tuple[NDArray[Any], FloatArray]:
    """For each grid time: the sample index before it and the fraction to the next one;
    -1 where the grid time is outside the samples or inside a gap over ``max_gap``."""
    right = np.searchsorted(st, grid, side="right")
    left = np.clip(right - 1, 0, st.size - 1)
    right = np.clip(right, 0, st.size - 1)
    span = st[right] - st[left]
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = np.where(span > 0, (grid - st[left]) / span, 0.0)
    bad = (grid < st[0]) | (grid > st[-1]) | (span > max_gap)
    return np.where(bad, -1, left), np.clip(frac, 0.0, 1.0)


def resample_quat(
    t: FloatArray, q: FloatArray, grid: FloatArray, max_gap: float = MAX_GAP_S
) -> FloatArray:
    """Quaternions sampled at ``t`` (n, 4), slerped onto ``grid``; NaN rows in gaps."""
    ok = np.isfinite(t) & np.all(np.isfinite(q), axis=1)
    t, q = t[ok], normalize(q[ok])
    out = np.full((grid.size, 4), np.nan)
    if t.size < 2:
        return out
    idx, frac = _gap_mask(t, grid, max_gap)
    good = idx >= 0
    i = idx[good]
    j = np.clip(i + 1, 0, t.size - 1)
    out[good] = slerp(q[i], q[j], frac[good])
    return out


# --- the artifact -------------------------------------------------------------------------


@dataclass
class Replay:
    columns: dict[str, NDArray[Any]]
    meta: dict[str, Any] = field(default_factory=dict)

    def write(self, directory: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        directory.mkdir(parents=True, exist_ok=True)
        table = pa.table({k: pa.array(v) for k, v in self.columns.items()})
        pq.write_table(table, directory / "replay.parquet", compression="zstd")
        (directory / "replay.json").write_text(
            json.dumps(self.meta, allow_nan=False), encoding="utf-8"
        )


def _sig(flight: Flight, *names: str) -> Signal | None:
    for name in names:
        sig = flight.signals.get(name)
        if sig is not None and np.isfinite(sig.values).sum() >= 2:
            return sig
    return None


def _span(flight: Flight) -> tuple[float, float] | None:
    """Time range covered by attitude or position."""
    starts, ends = [], []
    for name in ("att.q0", "att.roll", "pos.lat", "gps.lat", "pos.rel_alt"):
        sig = _sig(flight, name)
        if sig is not None:
            starts.append(float(sig.t[0]))
            ends.append(float(sig.t[-1]))
    if not starts:
        return None
    return min(starts), max(ends)


def _rate(flight: Flight) -> float:
    if flight.metadata.log_format != "tlog":
        return ONBOARD_RATE_HZ
    sig = _sig(flight, "att.roll", "att.q0", "pos.lat", "gps.lat")
    if sig is None or sig.t.size < 2:
        return TLOG_MAX_RATE_HZ
    native = 1.0 / max(float(np.median(np.diff(sig.t))), 1e-3)
    return float(min(TLOG_MAX_RATE_HZ, max(1.0, round(native))))


def _attitude(flight: Flight, grid: FloatArray) -> tuple[FloatArray, str | None]:
    if all(_sig(flight, f"att.q{i}") is not None for i in range(4)):
        q0 = flight.signal("att.q0")
        cols = [q0.values] + [
            resample(flight.signal(f"att.q{i}"), q0.t, MAX_GAP_S) for i in (1, 2, 3)
        ]
        return resample_quat(q0.t, np.stack(cols, axis=1), grid), "logged quaternion"
    roll = _sig(flight, "att.roll")
    pitch = _sig(flight, "att.pitch")
    if roll is None or pitch is None:
        return np.full((grid.size, 4), np.nan), None
    yaw = _sig(flight, "att.yaw")
    t = roll.t
    p = resample(pitch, t, MAX_GAP_S)
    if yaw is not None:
        y = resample(Signal("yaw", "deg", yaw.t, unwrap_yaw(yaw.values)), t, MAX_GAP_S)
        source = "roll, pitch and yaw"
    else:
        y = np.zeros_like(t)
        source = "roll and pitch (no heading logged)"
    q = euler_to_quat(roll.values, p, y)
    return resample_quat(t, q, grid), source


def _origin(flight: Flight, lat: Signal, lon: Signal) -> tuple[float, float, str]:
    """The take-off point (first sortie start), else the first valid position."""
    from flightlens.summary import summarize

    ok = np.isfinite(lat.values) & np.isfinite(lon.values)
    t, la, lo = lat.t[ok], lat.values[ok], resample(lon, lat.t, MAX_GAP_S)[ok]
    sorties = summarize(flight).sorties
    if sorties:
        when = float(sorties[0]["start"])
        i = int(np.clip(np.searchsorted(t, when), 0, t.size - 1))
        if np.isfinite(lo[i]):
            return float(la[i]), float(lo[i]), "take-off point"
    first = int(np.flatnonzero(np.isfinite(lo))[0]) if np.isfinite(lo).any() else 0
    return float(la[first]), float(lo[first]), "first position in the log"


def build_replay(flight: Flight) -> Replay:
    span = _span(flight)
    rate = _rate(flight)
    if span is None:
        start, end = 0.0, float(flight.metadata.log_duration or 0.0)
    else:
        start, end = span
    n = max(1, int(np.floor((end - start) * rate)) + 1)
    grid = start + np.arange(n) / rate
    cols: dict[str, NDArray[Any]] = {"t": grid}
    meta: dict[str, Any] = {
        "replay_version": REPLAY_VERSION,
        "rate_hz": rate,
        "start": start,
        "end": float(grid[-1]),
        "rows": int(n),
        "vehicle_type": flight.metadata.vehicle_type,
        "max_gap_s": MAX_GAP_S,
        "sources": {},
    }

    # position
    lat = _sig(flight, "pos.lat")
    lon = _sig(flight, "pos.lon")
    pos_source = "estimated position"
    if lat is None or lon is None:
        lat, lon, pos_source = _sig(flight, "gps.lat"), _sig(flight, "gps.lon"), "GPS receiver"
    nan = np.full(n, np.nan)
    if lat is not None and lon is not None:
        lat0, lon0, how = _origin(flight, lat, lon)
        la = resample(lat, grid, MAX_GAP_S)
        lo = resample(lon, grid, MAX_GAP_S)
        east, north = local_frame(la, lo, lat0, lon0)
        meta["origin"] = {"lat": lat0, "lon": lon0, "chosen": how}
        meta["sources"]["position"] = pos_source
    else:
        east, north = nan.copy(), nan.copy()
        meta["origin"] = None
        meta["sources"]["position"] = None
    rel = _sig(flight, "pos.rel_alt")
    up = resample(rel, grid, MAX_GAP_S) if rel is not None else nan.copy()
    amsl = _sig(flight, "pos.alt", "gps.alt")
    cols |= {
        "east": east,
        "north": north,
        "up": up,
        "alt_amsl": resample(amsl, grid, MAX_GAP_S) if amsl is not None else nan.copy(),
    }
    if meta["origin"] is not None and amsl is not None:
        meta["origin"]["alt_amsl"] = (
            float(np.nanmedian(cols["alt_amsl"][:1]))
            if np.isfinite(cols["alt_amsl"][:1]).any()
            else None
        )
    meta["sources"]["height"] = "height above take-off" if rel is not None else None

    # attitude
    q, att_source = _attitude(flight, grid)
    roll, pitch, yaw = quat_to_euler(q)
    cols |= {
        "qw": q[:, 0],
        "qx": q[:, 1],
        "qy": q[:, 2],
        "qz": q[:, 3],
        "roll": roll,
        "pitch": pitch,
        "yaw": yaw,
    }
    meta["sources"]["attitude"] = att_source

    # speeds and the rest of the instruments
    def col(*names: str) -> FloatArray:
        sig = _sig(flight, *names)
        return resample(sig, grid, MAX_GAP_S) if sig is not None else nan.copy()

    vn, ve = col("pos.vx"), col("pos.vy")
    ground = np.hypot(vn, ve)
    gps_speed = col("gps.speed")
    cols |= {
        "vn": vn,
        "ve": ve,
        "climb": col("pos.climb"),
        "ground_speed": np.where(np.isfinite(ground), ground, gps_speed),
        "airspeed": col("airspeed"),
        "thrust": col("ctrl.thrust", "ctrl.thr"),
        "bat_volt": col("bat.volt"),
        "bat_remaining": col("bat.remaining"),
    }
    motors = 0
    for i in range(1, MAX_MOTORS + 1):
        if _sig(flight, f"mot.{i}") is not None:
            cols[f"motor_{i}"] = col(f"mot.{i}")
            motors = i
    meta["motors"] = motors

    # discrete states: held, never interpolated
    def held(name: str) -> FloatArray:
        sig = flight.signals.get(name)
        return hold(sig, grid) if sig is not None and len(sig) else nan.copy()

    cols["gps_sats"] = held("gps.nsats")
    cols["gps_fix"] = held("gps.fix")
    cols["armed"] = flight.armed_mask(grid).astype(np.float64)
    modes: list[str] = []
    mode_index = np.full(n, -1.0)
    for e in sorted((e for e in flight.events if e.kind is EventKind.MODE), key=lambda e: e.t):
        if e.text not in modes:
            modes.append(e.text)
        mode_index[grid >= e.t] = modes.index(e.text)
    cols["mode_index"] = mode_index
    meta["modes"] = modes

    cols["valid_pos"] = (np.isfinite(east) & np.isfinite(north)).astype(np.float64)
    cols["valid_att"] = np.isfinite(q[:, 0]).astype(np.float64)
    meta["columns"] = list(cols)
    meta["extent"] = {
        k: [float(np.nanmin(cols[k])), float(np.nanmax(cols[k]))]
        if np.isfinite(cols[k]).any()
        else None
        for k in ("east", "north", "up")
    }
    meta["gaps"] = _gaps(grid, cols["valid_pos"], cols["valid_att"])
    return Replay(cols, meta)


def _gaps(grid: FloatArray, *valid: FloatArray) -> list[list[float]]:
    """Time ranges where data was expected but missing (inside the replay, not at its edges)."""
    from flightlens.intervals import intervals_where

    have = np.zeros(grid.size, dtype=bool)
    for v in valid:
        have |= v > 0
    if not have.any():
        return []
    first, last = np.flatnonzero(have)[[0, -1]]
    inner = np.zeros(grid.size, dtype=bool)
    inner[first : last + 1] = True
    missing = inner & ~np.all([v > 0 for v in valid if (v > 0).any()], axis=0)
    return [[a, b] for a, b in intervals_where(grid, missing, merge_gap=0.0)]


def read_columns(directory: Path) -> dict[str, NDArray[Any]] | None:
    import pyarrow.parquet as pq

    path = directory / "replay.parquet"
    if not path.is_file():
        return None
    table = pq.read_table(path)
    return {name: table.column(name).to_numpy() for name in table.column_names}
