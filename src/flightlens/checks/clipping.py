"""IMU clipping check.

Signal: ``vibe.clip``, a cumulative count of accelerometer clipping events
(ArduPilot VIBE.Clip0-2 summed; PX4 vehicle_imu_status.accel_clipping summed,
worst IMU). Clipping means the accelerometer saturated, so its readings,
and everything the estimator derives from them, were wrong at that moment.

Method: count the new clipping events in the air, outside impacts (a touchdown
or crash saturates the accelerometer by design; the crash check reports those).
Any increase is Attention, more than 100 is a Problem.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import impact_windows, in_air_mask, outside
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity


@register
class ImuClipping:
    id = "imu.clipping"
    title = "IMU clipping"
    version = "1.2.0"
    required_signals = ("vibe.clip",)
    defaults = {"attention": 0.0, "problem": 100.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        sig = flight.signal("vibe.clip")
        usable = (
            in_air_mask(flight, sig.t)
            & outside(sig.t, impact_windows(flight))
            & np.isfinite(sig.values)
        )
        if usable.sum() < 2:
            return [
                Finding(
                    check_id=self.id,
                    severity=Severity.NOT_EVALUATED,
                    title="IMU clipping not evaluated",
                    explanation="Fewer than two clipping samples in the air.",
                    evidence_signals=("vibe.clip",),
                )
            ]

        t, v = sig.t[usable], sig.values[usable]
        seg = flight.segment_ids(sig.t)[usable]
        steps = np.diff(v)
        # Counters only grow; a negative step is a reset (reboot, IMU switch).
        # A step across a disarmed gap between two flights is not counted.
        increases = np.where((steps > 0) & (seg[1:] == seg[:-1]), steps, 0.0)
        total = float(increases.sum())

        if total > config["problem"]:
            severity, limit = Severity.PROBLEM, config["problem"]
        elif total > config["attention"]:
            severity, limit = Severity.ATTENTION, config["attention"]
        else:
            return [
                Finding(
                    check_id=self.id,
                    severity=Severity.OK,
                    title="No IMU clipping",
                    explanation="The accelerometer never saturated in the air.",
                    measured=0.0,
                    unit="events",
                    evidence_signals=("vibe.clip",),
                )
            ]

        hits = np.flatnonzero(increases > 0)
        start, end = float(t[hits[0]]), float(t[hits[-1] + 1])
        return [
            Finding(
                check_id=self.id,
                severity=severity,
                title="Accelerometer clipping",
                explanation=(
                    f"The accelerometer saturated {total:,.0f} times in the air, "
                    f"so its readings were wrong at those moments."
                ),
                recommendation=(
                    "Reduce vibration: balance the propellers, check motor bearings and "
                    "soft-mount the flight controller."
                ),
                measured=total,
                threshold=limit,
                unit="events",
                start=start,
                end=end,
                evidence_signals=("vibe.clip",),
            )
        ]
