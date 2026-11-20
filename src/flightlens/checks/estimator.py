"""Estimator (EKF) health check.

Signals: innovation test ratios ``ekf.vel``, ``ekf.pos``, ``ekf.hgt``,
``ekf.mag``. A ratio of 1.0 means the estimator rejected that sensor's
measurement as inconsistent with its prediction.

Method: while in the air, each ratio held for at least 1 s. Attention > 0.5,
Problem > 1.0. The finding names the sensor with the highest ratio, because
that is where to look. The heading ratio is ignored for 10 s after each
takeoff, when PX4 re-aligns yaw in flight by design (observed on real logs).
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import grade, in_air_mask, takeoff_times
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity

SOURCES = {
    "ekf.vel": "velocity (GPS)",
    "ekf.pos": "position (GPS)",
    "ekf.hgt": "height (barometer or GPS)",
    "ekf.mag": "heading (magnetometer)",
}


@register
class EstimatorHealth:
    id = "estimator.health"
    title = "Estimator health"
    # PX4 leaves the GPS ratios empty when GPS is not fused (indoor, vision),
    # so any one ratio is enough to run; the others are used when present.
    required_signals = ("ekf.hgt",)
    alternative_signals = (("ekf.vel",), ("ekf.pos",), ("ekf.mag",))
    version = "1.2.0"
    defaults = {
        "attention": 0.5,
        "problem": 1.0,
        "min_duration_s": 1.0,
        "heading_settle_s": 10.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        breaches = []
        peaks: dict[str, float] = {}
        for name, label in SOURCES.items():
            if name not in flight.signals:
                continue
            sig = flight.signal(name)
            # On the ground the estimator is still aligning; judge it in the air.
            usable = in_air_mask(flight, sig.t) & np.isfinite(sig.values)
            if name == "ekf.mag":
                # PX4 re-aligns yaw in flight shortly after takeoff, which spikes
                # the heading ratio by design.
                for t0 in takeoff_times(flight):
                    usable &= ~((sig.t >= t0) & (sig.t < t0 + config["heading_settle_s"]))
            if usable.sum() < 2:
                continue
            peaks[name] = float(sig.values[usable].max())
            b = grade(
                sig.t,
                sig.values,
                usable,
                config["attention"],
                config["problem"],
                min_duration=config["min_duration_s"],
            )
            if b is not None:
                breaches.append((b, name, label))

        if not peaks:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Estimator not evaluated",
                    "No estimator test ratios while armed.",
                    evidence_signals=self.required_signals,
                )
            ]
        if not breaches:
            top = max(peaks, key=lambda k: peaks[k])
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Estimator healthy",
                    f"Highest innovation test ratio {peaks[top]:.2f} ({SOURCES[top]}).",
                    measured=peaks[top],
                    threshold=config["attention"],
                    evidence_signals=tuple(peaks),
                )
            ]

        b, name, label = max(breaches, key=lambda x: (x[0].severity.rank, x[0].worst))
        return [
            Finding(
                check_id=self.id,
                severity=b.severity,
                title=f"Estimator distrusted {label.split(' (')[0]}",
                explanation=(
                    f"The {label} innovation test ratio stayed above {b.limit:g} for "
                    f"{b.end - b.start:.0f} s, peaking at {b.worst:.2f}. Above 1.0 the "
                    "estimator rejects that sensor."
                ),
                recommendation=f"Investigate the {label} sensor and its mounting or interference.",
                measured=b.worst,
                threshold=b.limit,
                start=b.start,
                end=b.end,
                evidence_signals=(name,),
            )
        ]
