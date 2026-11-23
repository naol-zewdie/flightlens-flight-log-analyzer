"""Wind: how strong the wind was, from the estimator.

**wind.speed** — signals ``wind.n`` and ``wind.e`` (PX4 ``wind``, ArduPilot
``XKF2.VWN/VWE``), in the air, smoothed with a running median over
``window_s`` (10 s) so a gust or the estimator's reaction to an impact does
not count. Attention above ``attention`` (10 m/s, about 20 kt) for at least
the window. Strong wind is a condition of the flight, not a fault of the
aircraft, so it never raises Problem; it is reported so a crash or poor
tracking can be read against it. On real ArduPlane flights the wind averaged
0.2-2.5 m/s.

Impact windows (a crash, a hard landing) are left out: the estimator's wind
jumps there (13.5 m/s for 0.7 s at a real ArduPlane crash).

ArduCopter only estimates wind with ``EK3_DRAG_BCOEF_X/Y`` set; a wind
estimate stuck at 0 is *not evaluated*.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import held_windows, impact_windows, in_air_mask, outside, resample
from flightlens.checks.base import register
from flightlens.intervals import format_time
from flightlens.model import CheckConfig, Finding, Flight, Severity


def _running_median(t: np.ndarray, v: np.ndarray, window_s: float) -> np.ndarray:
    if t.size < 2:
        return v.copy()
    rate = (t.size - 1) / max(float(t[-1] - t[0]), 1e-9)
    n = max(1, round(window_s * rate))
    n -= 1 - n % 2  # odd
    n = min(n, t.size - (1 - t.size % 2))
    if n <= 1:
        return v.copy()
    padded = np.pad(v, n // 2, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, n)
    return np.asarray(np.median(windows, axis=1), dtype=np.float64)


@register
class WindSpeed:
    id = "wind.speed"
    title = "Wind"
    version = "1.0.0"
    required_signals = ("wind.n", "wind.e")
    defaults = {"attention": 10.0, "window_s": 10.0, "min_estimate": 0.05}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        n = flight.signal("wind.n")
        e = resample(flight.signal("wind.e"), n.t, 2.0)
        air = in_air_mask(flight, n.t) & np.isfinite(n.values) & np.isfinite(e)
        air &= outside(n.t, impact_windows(flight))  # the estimator jumps at an impact
        speed = np.hypot(n.values, e)
        if air.sum() < 5 or float(np.max(speed[air])) < config["min_estimate"]:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Wind not evaluated",
                    "No wind estimate in the air (ArduCopter estimates wind only with "
                    "EK3_DRAG_BCOEF_X/Y set).",
                    evidence_signals=("wind.n", "wind.e"),
                )
            ]
        t = n.t[air]
        smooth = _running_median(t, speed[air], config["window_s"])
        typical, top = float(np.median(speed[air])), float(np.max(smooth))
        # held for half the window: a burst at the edge of the data is not wind
        windows = held_windows(t, smooth > config["attention"], config["window_s"] / 2)
        if not windows:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Light wind",
                    f"The wind was typically {typical:.1f} m/s in the air, at most "
                    f"{max(top, typical):.1f} m/s over {config['window_s']:.0f} s.",
                    measured=top,
                    threshold=config["attention"],
                    unit="m/s",
                    evidence_signals=("wind.n", "wind.e"),
                )
            ]
        a, b = windows[0]
        return [
            Finding(
                self.id,
                Severity.ATTENTION,
                "Strong wind",
                f"The estimated wind reached {top:.1f} m/s (averaged over "
                f"{config['window_s']:.0f} s; typically {typical:.1f} m/s), first at "
                f"{format_time(a)}. Read tracking errors, high motor output and battery use "
                "against it.",
                recommendation=(
                    "Check that the wind was within the aircraft's limits; for planes, fly "
                    "faster than the wind with a margin."
                ),
                measured=top,
                threshold=config["attention"],
                unit="m/s",
                start=a,
                end=b,
                evidence_signals=("wind.n", "wind.e"),
            )
        ]
