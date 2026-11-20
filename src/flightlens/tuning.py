"""Rate-loop step response: how each axis follows its rate setpoint.

The flight never contains a clean step, but every stick input and correction
is an input to the rate controller and the gyro shows its output. Over short
windows of flight, the controller's impulse response is estimated by
regularised (Wiener) deconvolution of the measured rate by the setpoint, and
integrated into a step response; the median over all windows is robust to
windows where something else happened. This is the method of PID-Analyzer and
of PX4 Flight Review's step-response plot.

From the median response, per axis:

- **final**: where the response settles (1.0: the rate reaches what was asked);
- **overshoot**: how far the peak goes beyond ``final`` (ringing, too much P or
  too little D);
- **rise time**: time to 90% of ``final`` (a slow, soft response).

numpy only; nothing here is specific to PX4 or ArduPilot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from flightlens.checks._util import airframe, hold, impact_windows, in_air_mask, outside
from flightlens.model import Flight

FloatArray = NDArray[np.float64]

AXES = ("roll", "pitch", "yaw")
WINDOW_S = 2.0
MAX_FS = 500.0
MIN_FS = 20.0
REGULARISATION = 0.01
"""Wiener regularisation, relative to the mean input power in the window. Chosen on
simulated loops of known damping: 0.003 recovers the true peak within 0.03-0.08
(0.05 underestimated a 1.78 peak as 1.51)."""
COHERENCE_HZ = 3.0
"""Upper end of the band whose coherence decides whether the estimate is trusted:
where every rate loop, fast or sluggish, responds. Above a slow loop's bandwidth
the measured rate is mostly gyro noise, which would wrongly mark it unreliable."""


@dataclass
class AxisResponse:
    axis: str
    fs: float
    windows: int
    t: FloatArray
    step: FloatArray
    """Median step response over the windows."""
    low: FloatArray
    high: FloatArray
    """25th and 75th percentile over the windows: how consistent the estimate is."""
    final: float
    peak: float
    overshoot: float
    rise_s: float | None
    input_dps: float
    """Median setpoint activity (std, deg/s) of the windows used."""
    coherence: float = 0.0
    """How well the setpoint explains the measured rate (0..1); low: do not trust."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "fs": round(self.fs, 1),
            "windows": self.windows,
            "t": [round(float(x), 4) for x in self.t],
            "step": [round(float(x), 4) for x in self.step],
            "low": [round(float(x), 4) for x in self.low],
            "high": [round(float(x), 4) for x in self.high],
            "final": round(self.final, 3),
            "peak": round(self.peak, 3),
            "overshoot": round(self.overshoot, 3),
            "rise_s": round(self.rise_s, 4) if self.rise_s is not None else None,
            "input_dps": round(self.input_dps, 1),
            "coherence": round(self.coherence, 3),
        }


@dataclass
class Tuning:
    airframe: str
    response_s: float
    axes: dict[str, AxisResponse] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "airframe": self.airframe,
            "response_s": self.response_s,
            "axes": {k: v.to_dict() for k, v in self.axes.items()},
        }


def _runs(t: FloatArray, keep: NDArray[np.bool_], max_gap: float) -> list[tuple[float, float]]:
    """Contiguous stretches of kept samples without long gaps."""
    out = []
    start = None
    prev = None
    for ti, k in zip(t.tolist(), keep.tolist(), strict=True):
        if k and start is None:
            start = ti
        elif start is not None and (not k or ti - (prev or ti) > max_gap):
            if prev is not None and prev - start >= WINDOW_S:
                out.append((start, prev))
            start = ti if k else None
        prev = ti
    if start is not None and prev is not None and prev - start >= WINDOW_S:
        out.append((start, prev))
    return out


def _spectra(x: FloatArray, y: FloatArray) -> tuple[NDArray[np.complex128], ...]:
    n = x.size
    w = np.hanning(n)
    xf = np.fft.rfft((x - x.mean()) * w, 2 * n)
    yf = np.fft.rfft((y - y.mean()) * w, 2 * n)
    return xf, yf


def _deconvolve(
    xf: NDArray[np.complex128], yf: NDArray[np.complex128], n: int, n_resp: int, fs: float
) -> FloatArray:
    """Step response of the system that turned input spectrum ``xf`` into output ``yf``."""
    f = np.fft.rfftfreq(2 * n, 1 / fs)
    power = np.abs(xf) ** 2
    # regularise more above a cut-off, where the input has little energy and
    # the gyro mostly noise
    cutoff = min(25.0, 0.4 * fs / 2)
    lam = REGULARISATION * power.mean() * (1 + (f / cutoff) ** 4)
    transfer = yf * np.conj(xf) / (power + lam)
    impulse = np.fft.irfft(transfer, 2 * n)[:n_resp]
    return np.cumsum(impulse)


def coherence(
    xs: list[NDArray[np.complex128]], ys: list[NDArray[np.complex128]], f: FloatArray, hi: float
) -> float:
    """Mean magnitude-squared coherence of input and output over 0.5 Hz..``hi``.

    1 means the measured rate is explained by the setpoint (a linear loop);
    near 0 means it is not, and a step response estimated from it is noise.
    """
    sxx = sum(np.abs(x) ** 2 for x in xs)
    syy = sum(np.abs(y) ** 2 for y in ys)
    sxy = sum(y * np.conj(x) for x, y in zip(xs, ys, strict=True))
    c = np.abs(sxy) ** 2 / np.maximum(sxx * syy, 1e-30)
    band = (f >= 0.5) & (f <= hi)
    return float(np.mean(c[band])) if band.any() else 0.0


def analyze_axis(
    flight: Flight, axis: str, response_s: float, min_input_dps: float
) -> AxisResponse | None:
    sp_name, y_name = f"rate.des_{axis}", f"rate.{axis}"
    if not flight.has(sp_name, y_name):
        return None
    sp, y = flight.signal(sp_name), flight.signal(y_name)
    if len(sp) < 10 or len(y) < 10:
        return None
    dt = float(np.median(np.diff(sp.t)))
    if not np.isfinite(dt) or dt <= 0:
        return None
    fs = min(1.0 / dt, MAX_FS)
    if fs < MIN_FS:
        return None

    keep = in_air_mask(flight, sp.t) & outside(sp.t, impact_windows(flight))
    keep &= np.isfinite(sp.values)
    if "state.att_ctrl" in flight.signals:  # only while the controller stabilises (not Manual)
        keep &= hold(flight.signal("state.att_ctrl"), sp.t) == 1
    n_win = round(WINDOW_S * fs)
    n_resp = round(response_s * fs)
    ok_y = np.isfinite(y.values)
    if ok_y.sum() < 10:
        return None
    steps, inputs = [], []
    spec_x: list[NDArray[np.complex128]] = []
    spec_y: list[NDArray[np.complex128]] = []
    for a, b in _runs(sp.t, keep, max_gap=max(5 * dt, 0.1)):
        grid = np.arange(a, b, 1 / fs)
        xs = np.interp(grid, sp.t, sp.values)
        ys = np.interp(grid, y.t[ok_y], y.values[ok_y])
        for i in range(0, grid.size - n_win + 1, n_win // 2):
            xw, yw = xs[i : i + n_win], ys[i : i + n_win]
            activity = float(np.std(xw))
            if activity < min_input_dps:
                continue
            xf, yf = _spectra(xw, yw)
            steps.append(_deconvolve(xf, yf, n_win, n_resp, fs))
            spec_x.append(xf)
            spec_y.append(yf)
            inputs.append(activity)
    if len(steps) < 3:
        return None
    coh = coherence(spec_x, spec_y, np.fft.rfftfreq(2 * n_win, 1 / fs), min(COHERENCE_HZ, fs / 4))
    s = np.asarray(steps)
    step = np.median(s, axis=0)
    t = np.arange(n_resp) / fs
    tail = t >= 0.6 * response_s
    final = float(np.mean(step[tail]))
    peak = float(np.max(step))
    overshoot = peak / final - 1 if final > 0.2 else 0.0
    rise = None
    if final > 0.2:
        above = np.flatnonzero(step >= 0.9 * final)
        rise = float(t[above[0]]) if above.size else None
    return AxisResponse(
        axis=axis,
        fs=fs,
        windows=len(steps),
        t=t,
        step=step,
        low=np.percentile(s, 25, axis=0),
        high=np.percentile(s, 75, axis=0),
        final=final,
        peak=peak,
        overshoot=max(overshoot, 0.0),
        rise_s=rise,
        input_dps=float(np.median(inputs)),
        coherence=coh,
    )


def response_time(flight: Flight) -> float:
    """Length of the step response to show: fixed wing reacts slower."""
    return 1.0 if airframe(flight) == "fixed_wing" else 0.5


def analyze_tuning(flight: Flight, min_input_dps: float = 10.0) -> Tuning | None:
    """Step response per axis that has enough stick or correction activity."""
    response_s = response_time(flight)
    out = Tuning(airframe=airframe(flight), response_s=response_s)
    for axis in AXES:
        r = analyze_axis(flight, axis, response_s, min_input_dps)
        if r is not None:
            out.axes[axis] = r
    return out if out.axes else None
