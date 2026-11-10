"""Vibration level check.

Two signal sources, with their own thresholds because the metrics differ:

- ArduPilot: ``vibe.x/y/z`` (VIBE.VibeX/Y/Z, m/s²). Limits 30 / 60 m/s².
- PX4: ``vibe.metric`` (vehicle_imu_status.accel_vibration_metric, m/s²) from the
  worst IMU. The scale is smaller than ArduPilot's. Limits of 20 / 30 m/s², held
  for at least 3 s, were set from 25 public PX4 logs: healthy flights peaked
  below about 20, and a hexacopter with 27,935 clipping events sat at 21-29.

Method: while armed, find windows where the value stays above a limit for at
least ``min_duration_s`` (gaps up to 1 s are merged) and report the peak in the
worst window. All thresholds are community starting values, not
aircraft-approved limits; tune them per aircraft profile.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks.base import register
from flightlens.intervals import intervals_where
from flightlens.model import CheckConfig, Finding, Flight, Severity, Signal

_RECOMMENDATION = (
    "Balance or replace the propellers, check the motor bearings, "
    "and soft-mount the flight controller."
)
_MERGE_GAP_S = 1.0


@register
class VibrationLevel:
    id = "vibration.level"
    title = "Vibration level"
    version = "1.1.0"
    required_signals = ("vibe.x", "vibe.y", "vibe.z")
    alternative_signals = (("vibe.metric",),)
    defaults = {
        "attention": 30.0,
        "problem": 60.0,
        "min_duration_s": 0.0,
        "px4_attention": 20.0,
        "px4_problem": 30.0,
        "px4_min_duration_s": 3.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        if flight.has(*self.required_signals):
            series = [(f"{a.upper()} axis", flight.signal(f"vibe.{a}")) for a in "xyz"]
            att, prob, min_dur = (
                config["attention"],
                config["problem"],
                config["min_duration_s"],
            )
        else:
            series = [("worst IMU", flight.signal("vibe.metric"))]
            att, prob, min_dur = (
                config["px4_attention"],
                config["px4_problem"],
                config["px4_min_duration_s"],
            )

        findings: list[Finding] = []
        overall_peak = -np.inf
        evaluated = 0
        for label, sig in series:
            usable = flight.armed_mask(sig.t) & np.isfinite(sig.values)
            # An all-zero metric means the firmware did not compute it.
            if not usable.any() or not np.any(sig.values[usable] > 0):
                continue
            evaluated += 1
            overall_peak = max(overall_peak, float(sig.values[usable].max()))
            finding = self._worst(sig, usable, label, att, prob, min_dur)
            if finding is not None:
                findings.append(finding)

        if evaluated == 0:
            return [
                Finding(
                    check_id=self.id,
                    severity=Severity.NOT_EVALUATED,
                    title="Vibration not evaluated",
                    explanation=(
                        "No usable vibration samples while armed "
                        "(missing, invalid, or always zero in this log)."
                    ),
                    evidence_signals=tuple(s.name for _, s in series),
                )
            ]
        if not findings:
            findings.append(
                Finding(
                    check_id=self.id,
                    severity=Severity.OK,
                    title="Vibration normal",
                    explanation=f"Peak vibration {overall_peak:.1f} m/s² while armed.",
                    measured=overall_peak,
                    threshold=att,
                    unit="m/s²",
                    evidence_signals=tuple(s.name for _, s in series),
                )
            )
        return findings

    def _worst(
        self,
        sig: Signal,
        usable: np.ndarray,
        label: str,
        attention: float,
        problem: float,
        min_duration: float,
    ) -> Finding | None:
        for severity, limit in ((Severity.PROBLEM, problem), (Severity.ATTENTION, attention)):
            windows = [
                (a, b)
                for a, b in intervals_where(sig.t, usable & (sig.values > limit), _MERGE_GAP_S)
                if b - a >= min_duration
            ]
            if not windows:
                continue

            # Report the window that contains the highest value.
            def peak_in(w: tuple[float, float]) -> float:
                sel = usable & (sig.t >= w[0]) & (sig.t <= w[1])
                return float(sig.values[sel].max())

            start, end = max(windows, key=peak_in)
            peak = peak_in((start, end))
            held = f" for {end - start:.0f} s" if end - start >= 1 else ""
            return Finding(
                check_id=self.id,
                severity=severity,
                title=f"Vibration too high ({label})",
                explanation=(
                    f"Vibration ({label}) stayed above the {severity.value} limit of "
                    f"{limit:g} m/s²{held}, peaking at {peak:.1f} m/s²."
                ),
                recommendation=_RECOMMENDATION,
                measured=peak,
                threshold=limit,
                unit="m/s²",
                start=start,
                end=end,
                evidence_signals=(sig.name,),
            )
        return None
