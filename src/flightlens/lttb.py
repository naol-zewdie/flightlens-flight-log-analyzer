"""Largest-Triangle-Three-Buckets downsampling for plots.

Keeps the visual shape of a signal (peaks and dips survive) while cutting
millions of samples to a few thousand, so charts stay fast in the browser.
"""

from __future__ import annotations

import numpy as np

from flightlens.model import FloatArray


def lttb(t: FloatArray, v: FloatArray, n_out: int) -> tuple[FloatArray, FloatArray]:
    """Downsample ``(t, v)`` to at most ``n_out`` points. NaN samples are dropped."""
    keep = np.isfinite(v) & np.isfinite(t)
    t, v = t[keep], v[keep]
    n = t.size
    if n_out >= n or n_out < 3:
        return t, v

    out_idx = np.empty(n_out, dtype=np.int64)
    out_idx[0], out_idx[-1] = 0, n - 1
    edges = np.linspace(1, n - 1, n_out - 1).astype(np.int64)  # n_out - 2 buckets
    a = 0
    for i in range(n_out - 2):
        lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
        nlo, nhi = edges[i + 1], edges[i + 2] if i + 2 < edges.size else n
        if nhi <= nlo:
            nhi = nlo + 1
        avg_t = t[nlo:nhi].mean()
        avg_v = v[nlo:nhi].mean()
        bt, bv = t[lo:hi], v[lo:hi]
        area = np.abs((t[a] - avg_t) * (bv - v[a]) - (t[a] - bt) * (avg_v - v[a]))
        a = lo + int(np.argmax(area))
        out_idx[i + 1] = a
    return t[out_idx], v[out_idx]


def with_gaps(
    t: FloatArray, v: FloatArray, factor: float = 5.0
) -> tuple[list[float], list[float | None]]:
    """JSON-ready lists with a ``None`` inserted at gaps, so charts show breaks."""
    if t.size < 2:
        return t.tolist(), v.tolist()
    dt = np.diff(t)
    typical = float(np.median(dt))
    gap_after = np.flatnonzero(dt > max(typical * factor, 1e-9))
    ts: list[float] = t.tolist()
    vs: list[float | None] = v.tolist()
    for i in gap_after[::-1]:
        ts.insert(int(i) + 1, float((t[i] + t[i + 1]) / 2))
        vs.insert(int(i) + 1, None)
    return ts, vs
