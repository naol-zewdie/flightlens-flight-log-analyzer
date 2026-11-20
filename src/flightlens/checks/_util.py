"""Shared helpers for checks (not a check module; skipped by discovery)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from flightlens.intervals import intervals_where
from flightlens.model import BoolArray, Flight, FloatArray, Severity, Signal


def resample(sig: Signal, t: FloatArray, max_gap: float = 0.5) -> FloatArray:
    """Linearly interpolate ``sig`` onto times ``t``.

    Points outside the signal's time range, or between two samples more than
    ``max_gap`` seconds apart, become NaN, so gaps are never bridged silently.
    """
    valid = np.isfinite(sig.values)
    st, sv = sig.t[valid], sig.values[valid]
    out = np.full(t.shape, np.nan)
    if st.size < 2:
        return out
    out = np.interp(t, st, sv)
    right = np.clip(np.searchsorted(st, t), 0, st.size - 1)
    left = np.clip(right - 1, 0, st.size - 1)
    exact = st[right] == t
    gap = np.where(exact, 0.0, st[right] - st[left])
    out[(t < st[0]) | (t > st[-1]) | (gap > max_gap)] = np.nan
    return out


def hold(sig: Signal, t: FloatArray) -> FloatArray:
    """Zero-order hold: the most recent value of ``sig`` at each time in ``t``.

    Right for state flags (landed, control mode) that change in steps. Times
    before the first sample are NaN.
    """
    idx = np.searchsorted(sig.t, t, side="right") - 1
    out = sig.values[np.clip(idx, 0, max(sig.t.size - 1, 0))].astype(np.float64)
    out[idx < 0] = np.nan
    return out


MIN_CLIMB_M = 1.0
"""An armed period whose altitude never changes by this much never left the ground."""
IMPACT_VIBE = 60.0
"""ArduPilot VIBE level (m/s²) that, as a short burst, marks an impact, not vibration."""


def airframe(flight: Flight) -> str:
    """``fixed_wing``, ``vtol``, ``multirotor`` or ``other``, from the vehicle type."""
    vt = (flight.metadata.vehicle_type or "").lower()
    if "vtol" in vt:
        return "vtol"
    if "fixed wing" in vt or "plane" in vt:
        return "fixed_wing"
    if any(k in vt for k in ("rotor", "copter", "helicopter", "coaxial")):
        return "multirotor"
    return "other"


def climbed(flight: Flight) -> list[bool]:
    """For each armed segment, whether the aircraft actually left the ground.

    Uses the altitude above the takeoff point. A land detector alone can be
    fooled (for example a simulator whose vehicle never lifts off), so when the
    altitude is known a segment must also climb at least ``MIN_CLIMB_M``.
    Without altitude data every segment is assumed to have flown.
    """
    alt = flight.signals.get("pos.rel_alt")
    if alt is None:
        return [True] * len(flight.segments)
    out = []
    for seg in flight.segments:
        sel = (alt.t >= seg.start) & (alt.t <= seg.end) & np.isfinite(alt.values)
        vals = alt.values[sel]
        out.append(bool(vals.size) and float(vals.max() - vals.min()) >= MIN_CLIMB_M)
    return out


def in_air_mask(flight: Flight, t: FloatArray) -> BoolArray:
    """Armed, not landed, and in an armed period that actually left the ground."""
    mask = flight.armed_mask(t)
    if "state.landed" in flight.signals and len(flight.signals["state.landed"]):
        landed = hold(flight.signals["state.landed"], t)
        mask &= landed == 0
    for seg, flew in zip(flight.segments, climbed(flight), strict=True):
        if not flew:
            mask &= ~((t >= seg.start) & (t <= seg.end))
    return mask


def impact_windows(
    flight: Flight, pad_s: float = 2.0, max_len_s: float = 1.5
) -> list[tuple[float, float]]:
    """Short bursts of extreme vibration (touchdown, crash), padded by ``pad_s``.

    Uses ArduPilot's per-axis VIBE; a burst longer than ``max_len_s`` is
    sustained vibration, not an impact, and is not returned.
    """
    if not flight.has("vibe.x", "vibe.y", "vibe.z"):
        return []
    sx, sy, sz = (flight.signal(f"vibe.{a}") for a in "xyz")
    if not (sx.t.shape == sy.t.shape == sz.t.shape):
        return []
    worst = np.fmax(np.fmax(sx.values, sy.values), sz.values)
    bursts = intervals_where(sz.t, worst > IMPACT_VIBE, merge_gap=0.5)
    return [(a - pad_s, b + pad_s) for a, b in bursts if b - a <= max_len_s]


def outside(t: FloatArray, windows: list[tuple[float, float]]) -> BoolArray:
    mask = np.ones(t.shape, dtype=np.bool_)
    for a, b in windows:
        mask &= ~((t >= a) & (t <= b))
    return mask


def takeoff_times(flight: Flight) -> list[float]:
    """Times the land detector switched from landed to flying."""
    sig = flight.signals.get("state.landed")
    if sig is None or len(sig) < 2:
        return []
    v = sig.values
    idx = np.flatnonzero((v[:-1] == 1) & (v[1:] == 0))
    return [float(sig.t[i + 1]) for i in idx]


def held_windows(
    t: FloatArray, mask: BoolArray, min_duration: float, merge_gap: float = 1.0
) -> list[tuple[float, float]]:
    """Windows where ``mask`` holds for at least ``min_duration`` seconds."""
    return [(a, b) for a, b in intervals_where(t, mask, merge_gap) if b - a >= min_duration]


@dataclass(frozen=True)
class Breach:
    severity: Severity
    limit: float
    start: float
    end: float
    worst: float
    """Most extreme value inside the window."""


def grade(
    t: FloatArray,
    values: FloatArray,
    usable: BoolArray,
    attention: float,
    problem: float,
    *,
    above: bool = True,
    min_duration: float = 0.0,
    merge_gap: float = 1.0,
) -> Breach | None:
    """Find the most serious held breach of a two-level threshold.

    ``above=True`` means high values are bad (``>``); ``False`` means low values
    are bad (``<``). Returns the window containing the most extreme value.
    """
    for severity, limit in ((Severity.PROBLEM, problem), (Severity.ATTENTION, attention)):
        bad = usable & ((values > limit) if above else (values < limit))
        windows = held_windows(t, bad, min_duration, merge_gap)
        if not windows:
            continue

        def extreme(w: tuple[float, float]) -> float:
            sel = usable & (t >= w[0]) & (t <= w[1])
            seg = values[sel]
            return float(seg.max() if above else seg.min())

        start, end = max(windows, key=extreme) if above else min(windows, key=extreme)
        return Breach(severity, limit, start, end, extreme((start, end)))
    return None


def armed_usable(flight: Flight, sig: Signal) -> BoolArray:
    return flight.armed_mask(sig.t) & np.isfinite(sig.values)


def motor_signals(flight: Flight) -> list[Signal]:
    """Lift motors ``mot.1..N`` in order (pushers are excluded by the parser)."""
    names = sorted(
        (n for n in flight.signals if n.startswith("mot.")), key=lambda n: int(n.split(".")[1])
    )
    return [flight.signals[n] for n in names]
