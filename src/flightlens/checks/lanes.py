"""Estimator lane switches: the flight controller changed which estimator it trusts.

Signal: ``ekf.primary`` (PX4 ``estimator_selector_status.primary_instance``,
ArduPilot ``XKF4.PI``), in the air.

Flight controllers with several IMUs run one estimator (lane, core) per IMU and
fly on the one whose sensors agree best. A switch in flight means the active
one became worse than another: an IMU or magnetometer disagreed, or vibration
or a sensor fault upset it. One switch is Attention; ``problem_switches`` (3) or
more is Problem, a sign of a persistent sensor problem.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import in_air_mask
from flightlens.checks.base import register
from flightlens.intervals import format_time
from flightlens.model import CheckConfig, Finding, Flight, Severity


@register
class EstimatorLanes:
    id = "estimator.lanes"
    title = "Estimator lane switches"
    version = "1.0.0"
    required_signals = ("ekf.primary",)
    defaults = {"problem_switches": 3.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        sig = flight.signal("ekf.primary")
        ok = np.isfinite(sig.values)
        t, v = sig.t[ok], sig.values[ok]
        air = in_air_mask(flight, t)
        if air.sum() < 2:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Lane switches not evaluated",
                    "No estimator selection data while in the air.",
                    evidence_signals=("ekf.primary",),
                )
            ]
        # a switch counts when the sample after it is in the air
        idx = np.flatnonzero((np.diff(v) != 0) & air[1:]) + 1
        if idx.size == 0:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "No lane switches",
                    f"The flight controller stayed on estimator {int(v[air][0])} all flight.",
                    evidence_signals=("ekf.primary",),
                )
            ]
        times = [float(t[i]) for i in idx]
        changes = ", ".join(
            f"{int(v[i - 1])}→{int(v[i])} at {format_time(float(t[i]))}" for i in idx[:5]
        )
        n = len(times)
        sev = Severity.PROBLEM if n >= config["problem_switches"] else Severity.ATTENTION
        return [
            Finding(
                self.id,
                sev,
                "Estimator switched lanes in flight"
                if n == 1
                else f"Estimator switched lanes {n} times",
                f"The flight controller changed the estimator it flies on in the air ({changes}"
                f"{'…' if n > 5 else ''}). It does that when the active one's sensors disagree "
                "with the others: an IMU, compass or barometer problem, or vibration.",
                recommendation=(
                    "Compare the IMUs and compasses around the switch on the Signals tab; check "
                    "vibration, mounting and calibration of the sensor that the estimator left."
                ),
                measured=float(n),
                threshold=config["problem_switches"] if sev is Severity.PROBLEM else 1.0,
                unit="switches",
                start=times[0],
                end=times[-1],
                evidence_signals=("ekf.primary", "ekf.vel", "ekf.mag")
                if flight.has("ekf.vel", "ekf.mag")
                else ("ekf.primary",),
            )
        ]
