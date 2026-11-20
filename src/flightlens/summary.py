"""Flight summary: the numbers on the dashboard's summary cards.

Every value is optional. If the signals behind it are missing, the value is
None and the UI shows why, never a misleading zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from flightlens.model import EventKind, Flight

EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class FlightSummary:
    duration_s: float | None = None
    """Armed time (0 if it never armed); the whole log if arming is unknown."""
    airborne_s: float | None = None
    """Time actually in the air (armed, not landed, and the altitude changed)."""
    sorties: list[dict[str, float]] = field(default_factory=list)
    """Each airborne period: ``{"start": s, "end": s}``."""
    distance_m: float | None = None
    max_altitude_m: float | None = None
    """Highest point above the take-off altitude."""
    max_speed_m_s: float | None = None
    battery_used_mah: float | None = None
    modes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(flight: Flight) -> FlightSummary:
    sorties = _sorties(flight)
    return FlightSummary(
        duration_s=_duration(flight),
        airborne_s=sum(s["end"] - s["start"] for s in sorties) if sorties is not None else None,
        sorties=sorties or [],
        distance_m=_distance(flight),
        max_altitude_m=_max_altitude(flight),
        max_speed_m_s=_max_speed(flight),
        battery_used_mah=_battery_used(flight),
        modes=_modes(flight),
    )


def _duration(flight: Flight) -> float | None:
    if flight.segments:
        return sum(s.duration for s in flight.segments)
    if flight.arming_known:
        return 0.0
    return flight.metadata.log_duration


def _sorties(flight: Flight) -> list[dict[str, float]] | None:
    """Airborne periods, or None when the log has nothing to tell it from."""
    from flightlens.checks._util import in_air_mask
    from flightlens.intervals import intervals_where

    base = next(
        (flight.signals[n] for n in ("att.roll", "pos.rel_alt") if n in flight.signals), None
    )
    if base is None or base.t.size < 2:
        return None
    air = in_air_mask(flight, base.t)
    return [
        {"start": a, "end": b}
        for a, b in intervals_where(base.t, air, merge_gap=5.0)
        if b - a >= 2.0
    ]


def _armed(flight: Flight, name: str) -> tuple[np.ndarray, np.ndarray] | None:
    if name not in flight.signals:
        return None
    sig = flight.signals[name]
    keep = flight.armed_mask(sig.t) & np.isfinite(sig.values)
    if not keep.any():
        return None
    return sig.t[keep], sig.values[keep]


def _same_segment(flight: Flight, name: str) -> np.ndarray:
    """For consecutive armed samples, True when both are in the same flight."""
    sig = flight.signals[name]
    keep = flight.armed_mask(sig.t) & np.isfinite(sig.values)
    seg = flight.segment_ids(sig.t)[keep]
    return np.asarray(seg[1:] == seg[:-1])


def _distance(flight: Flight) -> float | None:
    for prefix in ("pos", "gps"):
        lat = _armed(flight, f"{prefix}.lat")
        lon = _armed(flight, f"{prefix}.lon")
        if lat is None or lon is None or lat[1].size != lon[1].size or lat[1].size < 2:
            continue
        la, lo = np.radians(lat[1]), np.radians(lon[1])
        dlat, dlon = np.diff(la), np.diff(lo)
        a = np.sin(dlat / 2) ** 2 + np.cos(la[:-1]) * np.cos(la[1:]) * np.sin(dlon / 2) ** 2
        legs = 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))
        same = _same_segment(flight, f"{prefix}.lat")
        if same.size == legs.size:
            legs = legs[same]
        return float(np.sum(legs))
    return None


def _max_altitude(flight: Flight) -> float | None:
    rel = _armed(flight, "pos.rel_alt")
    if rel is None:
        return None
    return float(rel[1].max() - rel[1][0])


def _max_speed(flight: Flight) -> float | None:
    vx, vy = _armed(flight, "pos.vx"), _armed(flight, "pos.vy")
    if vx is not None and vy is not None and vx[1].size == vy[1].size:
        return float(np.hypot(vx[1], vy[1]).max())
    gs = _armed(flight, "gps.speed")
    return float(gs[1].max()) if gs is not None else None


def _battery_used(flight: Flight) -> float | None:
    used = _armed(flight, "bat.used_mah")
    if used is None or used[1].size < 2:
        return None
    steps = np.diff(used[1])
    same = _same_segment(flight, "bat.used_mah")
    # Only consumption while armed; a negative step is a counter reset.
    return float(np.sum(np.where(same & (steps > 0), steps, 0.0)))


def _modes(flight: Flight) -> list[str]:
    """Modes active at any time while armed, in order of first use.

    A mode counts from when it was selected until the next mode change, so one
    chosen before arming and kept for the whole flight is included.
    """
    modes = [e for e in flight.events if e.kind is EventKind.MODE]
    seen: list[str] = []
    for i, e in enumerate(modes):
        until = modes[i + 1].t if i + 1 < len(modes) else float("inf")
        active = not flight.segments or any(
            e.t <= s.end and until >= s.start for s in flight.segments
        )
        if active and e.text not in seen:
            seen.append(e.text)
    return seen
