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


def in_air_mask(flight: Flight, t: FloatArray) -> BoolArray:
    """Armed and not landed. Uses the land detector when the log has it."""
    mask = flight.armed_mask(t)
    if "state.landed" in flight.signals and len(flight.signals["state.landed"]):
        landed = hold(flight.signals["state.landed"], t)
        mask &= landed == 0
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
