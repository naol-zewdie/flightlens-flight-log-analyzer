"""Helpers for turning per-sample conditions into time windows."""

from __future__ import annotations

import numpy as np

from flightlens.model import BoolArray, FloatArray


def intervals_where(
    t: FloatArray, mask: BoolArray, merge_gap: float = 0.0
) -> list[tuple[float, float]]:
    """Return ``(start, end)`` time windows where ``mask`` is true.

    Consecutive true samples form one window. Windows separated by at most
    ``merge_gap`` seconds are merged into one.
    """
    if t.shape != mask.shape:
        raise ValueError("t and mask must have the same shape")
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]]))

    out: list[tuple[float, float]] = []
    for s, e in zip(starts, ends, strict=True):
        a, b = float(t[s]), float(t[e])
        if out and a - out[-1][1] <= merge_gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def format_time(seconds: float | None) -> str:
    """Format seconds as ``mm:ss`` (or ``h:mm:ss`` for long logs)."""
    if seconds is None:
        return "--:--"
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
