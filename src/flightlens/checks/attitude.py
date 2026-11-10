"""Attitude tracking check.

Signals: ``att.roll/pitch/yaw`` (measured) and ``att.des_roll/pitch/yaw``
(setpoint), in degrees. The setpoint is resampled onto the measurement times;
gaps over 0.5 s are never bridged.

Method: absolute tracking error while in the air (armed and not landed) and
while the attitude controller is active (not Acro or Manual), held for at
least 2 s.
Roll and pitch: Attention > 5°, Problem > 10°. Yaw tracks more loosely on most
multirotors (yaw authority is weaker), so its default limits are 15° / 30°;
tune them per aircraft profile.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import grade, hold, in_air_mask, resample
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity


@register
class AttitudeTracking:
    id = "attitude.tracking"
    title = "Attitude tracking"
    version = "1.0.0"
    required_signals = ("att.roll", "att.pitch", "att.des_roll", "att.des_pitch")
    defaults = {
        "attention": 5.0,
        "problem": 10.0,
        "yaw_attention": 15.0,
        "yaw_problem": 30.0,
        "min_duration_s": 2.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        axes = [("roll", config["attention"], config["problem"])]
        axes.append(("pitch", config["attention"], config["problem"]))
        if flight.has("att.yaw", "att.des_yaw"):
            axes.append(("yaw", config["yaw_attention"], config["yaw_problem"]))

        findings: list[Finding] = []
        worst_ok: dict[str, float] = {}
        for axis, att, prob in axes:
            meas = flight.signal(f"att.{axis}")
            des = resample(flight.signal(f"att.des_{axis}"), meas.t)
            err = meas.values - des
            if axis == "yaw":
                err = (err + 180.0) % 360.0 - 180.0
            err = np.abs(err)
            usable = in_air_mask(flight, meas.t) & np.isfinite(err)
            if "state.att_ctrl" in flight.signals:
                usable &= hold(flight.signal("state.att_ctrl"), meas.t) == 1
            if usable.sum() < 10:
                continue
            worst_ok[axis] = float(np.percentile(err[usable], 99))
            b = grade(
                meas.t,
                err,
                usable,
                att,
                prob,
                min_duration=config["min_duration_s"],
                merge_gap=0.05,
            )
            if b is None:
                continue
            findings.append(
                Finding(
                    check_id=self.id,
                    severity=b.severity,
                    title=f"Poor {axis} tracking",
                    explanation=(
                        f"The measured {axis} differed from the commanded {axis} by more than "
                        f"{b.limit:g}° for {b.end - b.start:.0f} s, up to {b.worst:.1f}°."
                    ),
                    recommendation=(
                        "Check for a weak motor, damaged propeller or loose frame, then review "
                        "the rate controller tuning."
                    ),
                    measured=b.worst,
                    threshold=b.limit,
                    unit="°",
                    start=b.start,
                    end=b.end,
                    evidence_signals=(f"att.{axis}", f"att.des_{axis}"),
                )
            )

        if not worst_ok:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Attitude tracking not evaluated",
                    "Not enough attitude and setpoint samples in the air with the attitude "
                    "controller active.",
                    evidence_signals=self.required_signals,
                )
            ]
        if not findings:
            text = ", ".join(f"{a} {v:.1f}°" for a, v in worst_ok.items())
            findings.append(
                Finding(
                    self.id,
                    Severity.OK,
                    "Attitude tracking good",
                    f"99th-percentile tracking error: {text}.",
                    measured=max(worst_ok.values()),
                    unit="°",
                    evidence_signals=self.required_signals,
                )
            )
        return findings
