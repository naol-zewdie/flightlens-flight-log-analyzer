"""Gyro noise spectrum: where the noise is, whether it follows throttle, what filter fits.

Input: ``gyro.raw.x/y/z`` (deg/s, before the flight controller's filters) from
the parsers. Only time in the air counts, minus impacts. The log is split into
evenly sampled runs (gaps and bursts break a run), each run is cut into
Hann-windowed frames with 50% overlap, and the frames are averaged (Welch).

- **Peak**: the strongest narrow peak above ``MIN_HZ``, measured as its height
  over the median of the band (dB) and its RMS amplitude (deg/s).
- **Throttle tracking**: the peak frequency in each throttle band. Motor and
  propeller noise rises with throttle (a throttle- or RPM-referenced notch
  fits); frame resonance stays put (a fixed notch fits).
- **Spectrogram**: noise over time for the UI.

numpy only; nothing here is specific to PX4 or ArduPilot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import numpy as np
from numpy.typing import NDArray

from flightlens.checks._util import impact_windows, in_air_mask, outside
from flightlens.model import Flight

FloatArray = NDArray[np.float64]

AXES = ("x", "y", "z")
MIN_HZ = 30.0
"""Below this is flight motion and control, not noise a notch filter should target."""
MIN_AIR_S = 10.0
MAX_FRAMES = 6000
SPECTROGRAM_T = 240
SPECTROGRAM_F = 160


@dataclass
class Spectrum:
    source: str
    fs: float
    air_s: float
    freqs: FloatArray
    psd: dict[str, FloatArray]
    """Per axis, (deg/s)²/Hz."""
    peak_hz: float | None = None
    peak_db: float = 0.0
    peak_rms: float = 0.0
    peak_axis: str | None = None
    harmonics: list[float] = field(default_factory=list)
    tracking: list[tuple[float, float]] = field(default_factory=list)
    """(throttle 0..1, peak Hz) per throttle band."""
    tracks_throttle: bool | None = None
    hover_throttle: float | None = None
    hover_hz: float | None = None
    """Peak frequency at hover throttle, when the peak follows throttle."""
    spec_t: FloatArray = field(default_factory=lambda: np.zeros(0))
    spec_f: FloatArray = field(default_factory=lambda: np.zeros(0))
    spec_db: FloatArray = field(default_factory=lambda: np.zeros((0, 0)))

    @property
    def nyquist(self) -> float:
        return self.fs / 2

    def to_dict(self) -> dict[str, Any]:
        def r(a: FloatArray, n: int = 4) -> list[float]:
            return [round(float(x), n) for x in a]

        return {
            "source": self.source,
            "fs": round(self.fs, 1),
            "air_s": round(self.air_s, 1),
            "freqs": r(self.freqs, 2),
            "psd_db": {a: r(10 * np.log10(np.maximum(p, 1e-12)), 2) for a, p in self.psd.items()},
            "peak_hz": self.peak_hz,
            "peak_db": round(self.peak_db, 1),
            "peak_rms": round(self.peak_rms, 2),
            "peak_axis": self.peak_axis,
            "harmonics": self.harmonics,
            "tracking": [[round(a, 3), round(b, 1)] for a, b in self.tracking],
            "tracks_throttle": self.tracks_throttle,
            "hover_throttle": self.hover_throttle,
            "hover_hz": self.hover_hz,
            "spectrogram": {
                "t": r(self.spec_t, 1),
                "f": r(self.spec_f, 1),
                "db": [r(row, 1) for row in self.spec_db],
            },
        }


def throttle(flight: Flight) -> tuple[FloatArray, FloatArray] | None:
    """Collective throttle 0..1: commanded thrust, else the mean motor output."""
    for name in ("ctrl.thr", "ctrl.thrust"):
        if name in flight.signals and len(flight.signals[name]) > 1:
            s = flight.signals[name]
            return s.t, np.clip(s.values, 0, 1)
    motors = sorted(n for n in flight.signals if n.startswith("mot.") and n[4:].isdigit())
    if not motors:
        return None
    ref = flight.signals[motors[0]]
    vals = [np.interp(ref.t, flight.signals[m].t, flight.signals[m].values) for m in motors]
    return ref.t, np.clip(np.mean(vals, axis=0), 0, 1)


def _runs(t: FloatArray, keep: NDArray[np.bool_], fs: float) -> list[tuple[int, int]]:
    """Index ranges [a, b) of evenly sampled kept samples."""
    dt = np.diff(t)
    nominal = 1 / fs
    breaks = (dt > 1.5 * nominal) | (dt < 0.5 * nominal)
    good = keep.copy()
    starts: list[int] = []
    ends: list[int] = []
    edge = np.flatnonzero(np.diff(np.r_[0, good.astype(np.int8), 0]) != 0)
    for a, b in zip(edge[::2], edge[1::2], strict=True):
        cuts = a + 1 + np.flatnonzero(breaks[a : b - 1])
        bounds = [a, *cuts.tolist(), b]
        for s, e in pairwise(bounds):
            starts.append(s)
            ends.append(e)
    return list(zip(starts, ends, strict=True))


def _frame_count(n: int, nperseg: int, step: int) -> int:
    return 0 if n < nperseg else 1 + (n - nperseg) // step


def analyze_spectrum(flight: Flight) -> Spectrum | None:
    """None when the log has no usable high-rate gyro or too little air time."""
    if not all(f"gyro.raw.{a}" in flight.signals for a in AXES):
        return None
    gx = flight.signals["gyro.raw.x"]
    t = gx.t
    data = np.stack([flight.signals[f"gyro.raw.{a}"].values for a in AXES])
    if t.size < 256:
        return None
    fs = float(1 / np.median(np.diff(t)))
    keep = (
        in_air_mask(flight, t) & outside(t, impact_windows(flight)) & np.all(np.isfinite(data), 0)
    )
    # about 1.5 Hz resolution, at least 64 samples per frame
    nperseg = int(2 ** max(6, round(np.log2(fs / 1.5))))
    runs = _runs(t, keep, fs)
    longest = max((b - a for a, b in runs), default=0)
    if longest < nperseg:  # short bursts (batch sampler): fit the frame to them
        nperseg = int(2 ** int(np.log2(max(longest, 1))))
    if nperseg < 64:
        return None
    step = nperseg // 2
    runs = [(a, b) for a, b in runs if b - a >= nperseg]
    total = sum(_frame_count(b - a, nperseg, step) for a, b in runs)
    air_s = sum((b - a) for a, b in runs) / fs
    if total == 0 or air_s < MIN_AIR_S:
        return None

    window = np.hanning(nperseg)
    scale = 2.0 / (fs * float(np.sum(window**2)))
    stride = max(1, int(np.ceil(total / MAX_FRAMES)))
    frames_psd: list[FloatArray] = []
    frames_t: list[FloatArray] = []
    k = 0
    for a, b in runs:
        n = _frame_count(b - a, nperseg, step)
        pick = np.arange(n)[(k + np.arange(n)) % stride == 0]
        k += n
        if pick.size == 0:
            continue
        starts = a + pick * step
        idx = starts[:, None] + np.arange(nperseg)[None, :]
        seg = data[:, idx]  # (3, frames, nperseg)
        seg = seg - seg.mean(axis=2, keepdims=True)
        spec = np.abs(np.fft.rfft(seg * window, axis=2)) ** 2 * scale
        frames_psd.append(spec)
        frames_t.append(t[starts + nperseg // 2])
    psd_frames = np.concatenate(frames_psd, axis=1)  # (3, F, bins)
    ft = np.concatenate(frames_t)
    freqs = np.fft.rfftfreq(nperseg, 1 / fs)
    psd = psd_frames.mean(axis=1)
    out = Spectrum(
        source=gx.source,
        fs=fs,
        air_s=float(air_s),
        freqs=freqs,
        psd={a: psd[i] for i, a in enumerate(AXES)},
    )
    _find_peak(out, psd)
    _throttle_tracking(out, flight, psd_frames.sum(axis=0), ft)
    _spectrogram(out, psd_frames.sum(axis=0), ft)
    return out


def _smooth(x: FloatArray, n: int = 3) -> FloatArray:
    return np.convolve(x, np.ones(n) / n, mode="same")


def _peak_in(freqs: FloatArray, p: FloatArray, lo: float, hi: float) -> tuple[float, float] | None:
    """(frequency, height over the band median in dB) of the strongest bin in [lo, hi]."""
    band = (freqs >= lo) & (freqs <= hi)
    if band.sum() < 5:
        return None
    ps = _smooth(p)[band]
    i = int(np.argmax(ps))
    floor = float(np.median(ps))
    if floor <= 0:
        return None
    return float(freqs[band][i]), float(10 * np.log10(ps[i] / floor))


def _find_peak(out: Spectrum, psd: FloatArray) -> None:
    hi = 0.95 * out.nyquist
    total = psd.sum(axis=0)
    found = _peak_in(out.freqs, total, MIN_HZ, hi)
    if found is None:
        return
    f0, db = found
    out.peak_hz, out.peak_db = round(f0, 1), db
    df = float(out.freqs[1] - out.freqs[0])
    near = np.abs(out.freqs - f0) <= max(3 * df, 0.08 * f0)
    rms = {a: float(np.sqrt(np.sum(p[near]) * df)) for a, p in out.psd.items()}
    out.peak_axis = max(rms, key=lambda a: rms[a])
    out.peak_rms = rms[out.peak_axis]
    smooth = _smooth(total)
    floor = float(np.median(smooth[(out.freqs >= MIN_HZ) & (out.freqs <= hi)]))
    for h in (2, 3):
        lo_h, hi_h = h * f0 * 0.92, h * f0 * 1.08
        if hi_h > hi:
            break
        band = (out.freqs >= lo_h) & (out.freqs <= hi_h)
        if not np.any(band):
            continue
        i = int(np.argmax(np.where(band, smooth, -np.inf)))
        if 10 * np.log10(smooth[i] / floor) >= 6:  # a clear bump over the band median
            out.harmonics.append(round(float(out.freqs[i]), 1))


def _throttle_tracking(out: Spectrum, flight: Flight, frames: FloatArray, ft: FloatArray) -> None:
    thr = throttle(flight)
    if thr is None or out.peak_hz is None:
        return
    level = np.interp(ft, thr[0], thr[1])
    out.hover_throttle = round(float(np.median(level)), 3)
    edges = np.unique(np.quantile(level, np.linspace(0, 1, 6)))
    if edges.size < 3 or edges[-1] - edges[0] < 0.08:
        return  # throttle hardly changed: cannot tell motor noise from resonance
    lo, hi = 0.5 * out.peak_hz, min(2.0 * out.peak_hz, 0.95 * out.nyquist)
    for a, b in pairwise(edges.tolist()):
        sel = (level >= a) & (level <= b)
        if sel.sum() < 4:
            continue
        found = _peak_in(out.freqs, frames[sel].mean(axis=0), lo, hi)
        if found is not None and found[1] >= 6:
            out.tracking.append((float(np.median(level[sel])), found[0]))
    p10, p90 = np.quantile(level, [0.1, 0.9])
    if len(out.tracking) >= 3 and p90 - p10 >= 0.15:  # only judge when throttle really varied
        th = np.array([x for x, _ in out.tracking])
        fr = np.array([y for _, y in out.tracking])
        spread = (fr.max() - fr.min()) / fr.mean()
        corr = float(np.corrcoef(np.sqrt(th), fr)[0, 1]) if np.std(fr) > 0 else 0.0
        out.tracks_throttle = bool(spread > 0.12 and corr > 0.8)
        if out.tracks_throttle and out.hover_throttle is not None:
            order = np.argsort(th)
            out.hover_hz = round(float(np.interp(out.hover_throttle, th[order], fr[order])), 1)


def _spectrogram(out: Spectrum, frames: FloatArray, ft: FloatArray) -> None:
    """Average frames into at most SPECTROGRAM_T time bins and SPECTROGRAM_F bands."""
    nt = min(SPECTROGRAM_T, ft.size)
    t_edges = np.linspace(ft.min(), ft.max() + 1e-9, nt + 1)
    tb = np.clip(np.searchsorted(t_edges, ft, side="right") - 1, 0, nt - 1)
    fmask = out.freqs <= 0.95 * out.nyquist
    freqs = out.freqs[fmask]
    nf = min(SPECTROGRAM_F, freqs.size)
    fb = np.minimum((np.arange(freqs.size) * nf) // freqs.size, nf - 1)
    grid = np.zeros((nt, nf))
    counts = np.zeros(nt)
    np.add.at(counts, tb, 1)
    per_t = np.zeros((nt, freqs.size))
    np.add.at(per_t, tb, frames[:, fmask])
    for j in range(nf):
        grid[:, j] = per_t[:, fb == j].mean(axis=1)
    filled = counts > 0
    grid[filled] /= counts[filled, None]
    db = 10 * np.log10(np.maximum(grid, 1e-12))
    db[~filled] = np.nan
    out.spec_t = 0.5 * (t_edges[:-1] + t_edges[1:])
    out.spec_f = np.array([freqs[fb == j].mean() for j in range(nf)])
    out.spec_db = db
