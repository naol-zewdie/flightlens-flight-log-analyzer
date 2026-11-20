"""Rate-loop tuning: the step response of the roll and pitch rate controllers.

Signals: ``rate.des_<axis>`` and ``rate.<axis>`` (deg/s), in the air and, when
the log says so, only while the flight controller stabilises (not Manual).

The step response is estimated from normal flying (``flightlens.tuning``). It
is judged only when the estimate is trustworthy: at least ``min_windows``
two-second windows with stick or correction activity, and a setpoint that
explains the measured rate (coherence ≥ ``min_coherence``). Yaw is shown in the
web app but not judged: its loop is normally much slower.

Calibration: on simulated loops of known damping the estimated peak is within
0.1 of the true one; on 20 public PX4 flights the well-flown ones settle at
0.9-1.1 with at most +0.2 of overshoot. Estimates from fewer than 20 windows
were too noisy on real 50 Hz logs (spurious peaks of 1.3-1.6) to be judged.

- **Overshoot**: the peak above where the response settles (or above 1.0, if it
  settles lower) exceeds ``overshoot_attention`` (+0.3) — Attention; above
  ``overshoot_problem`` (+0.6) — Problem. A loop that settles above 1 without
  ringing is not overshoot.
- **Does not reach the setpoint**: settles below ``final_low`` (0.7) — Attention.
- **Does not follow the setpoint**: settles outside ``final_min``..``final_max``
  (0.5-1.6 times) — Attention: the controller is not in control of this axis
  (actuators saturated, out of trim, feed-forward wrong), or the logged
  setpoint is not the one flown.
- **Sluggish**: 90% of the final value takes longer than ``rise_attention_s``
  (0.3 s multicopter, 0.8 s fixed wing) — Attention. A sluggish loop has not
  settled within the window, so it is not also reported as falling short.
"""

from __future__ import annotations

from flightlens.checks._util import airframe
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity
from flightlens.tuning import AxisResponse, analyze_tuning

JUDGED_AXES = ("roll", "pitch")


@register
class RateTuning:
    id = "control.tuning"
    title = "Rate-loop tuning"
    version = "1.0.0"
    required_signals = ("rate.roll", "rate.des_roll")
    alternative_signals = (("rate.pitch", "rate.des_pitch"),)
    defaults = {
        "min_windows": 20.0,
        "min_coherence": 0.6,
        "min_input_dps": 5.0,
        "overshoot_attention": 0.3,
        "overshoot_problem": 0.6,
        "final_low": 0.7,
        "final_min": 0.5,
        "final_max": 1.6,
        "rise_attention_s": 0.3,
        "fw_rise_attention_s": 0.8,
    }
    missing_explanation = (
        "The log has no rate setpoint and measured rate, so the rate controllers' step "
        "response cannot be estimated."
    )

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        tuning = analyze_tuning(flight, min_input_dps=config["min_input_dps"])
        judged: list[AxisResponse] = []
        skipped: list[str] = []
        for axis in JUDGED_AXES:
            r = tuning.axes.get(axis) if tuning else None
            if r is None:
                skipped.append(f"{axis}: too little stick or correction activity in the air")
            elif r.windows < config["min_windows"] or r.coherence < config["min_coherence"]:
                skipped.append(
                    f"{axis}: estimate not reliable ({r.windows} windows, "
                    f"coherence {r.coherence:.2f})"
                )
            else:
                judged.append(r)
        if not judged:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Rate-loop tuning not evaluated",
                    "The step response could not be estimated reliably: "
                    + "; ".join(skipped)
                    + ". It needs a few minutes of flight with stick inputs or gusts.",
                    evidence_signals=("rate.des_roll", "rate.roll"),
                )
            ]

        fixed_wing = airframe(flight) == "fixed_wing"
        rise_limit = config["fw_rise_attention_s"] if fixed_wing else config["rise_attention_s"]
        findings = []
        for r in judged:
            evidence = (f"rate.des_{r.axis}", f"rate.{r.axis}")
            basis = (
                f"Estimated from {r.windows} two-second windows of flight "
                f"(coherence {r.coherence:.2f})."
            )
            name = r.axis.capitalize()
            if not config["final_min"] <= r.final <= config["final_max"]:
                findings.append(
                    Finding(
                        self.id,
                        Severity.ATTENTION,
                        f"{name} rate does not follow its setpoint",
                        f"The measured {r.axis} rate settles at {r.final:.2f}× the commanded rate "
                        f"instead of about 1. {basis} Either the controller is not in control of "
                        "this axis (control surfaces or motors at their limit, out of trim, "
                        "feed-forward far off), or the logged setpoint is not the one flown.",
                        recommendation=(
                            "Look at the setpoint and the measured rate together on the Signals "
                            "tab; check actuator saturation and trim before retuning."
                        ),
                        measured=r.final,
                        threshold=config["final_max"] if r.final > 1 else config["final_min"],
                        comparison=">" if r.final > 1 else "<",
                        unit="×",
                        evidence_signals=evidence,
                    )
                )
                continue
            sluggish = r.rise_s is not None and r.rise_s > rise_limit
            # ringing above where the response settles (or above 1, if it settles lower)
            excess = r.peak - max(r.final, 1.0)
            if excess > config["overshoot_attention"]:
                sev = (
                    Severity.PROBLEM if excess > config["overshoot_problem"] else Severity.ATTENTION
                )
                findings.append(
                    Finding(
                        self.id,
                        sev,
                        f"{name} rate loop overshoots",
                        f"After a change in setpoint the {r.axis} rate peaks at {r.peak:.2f}× "
                        f"the commanded rate, {excess:+.2f} above where it settles "
                        f"({r.final:.2f}×). {basis} Overshoot means the loop rings: too much P "
                        "(or feed-forward) for the D that damps it.",
                        recommendation=(
                            f"Lower the {r.axis} rate P gain, or raise D, a step at a time; on "
                            "PX4 MC_ROLLRATE_P/D, on ArduPilot ATC_RAT_RLL_P/D (Plane: "
                            "RLL_RATE_P/D). Re-check after each change."
                        ),
                        measured=excess,
                        threshold=config["overshoot_problem"]
                        if sev is Severity.PROBLEM
                        else config["overshoot_attention"],
                        unit="× over",
                        evidence_signals=evidence,
                    )
                )
            if r.final < config["final_low"] and not sluggish:
                findings.append(
                    Finding(
                        self.id,
                        Severity.ATTENTION,
                        f"{name} rate falls short of its setpoint",
                        f"The {r.axis} rate settles at only {r.final:.2f}× the commanded rate. "
                        f"{basis} The loop does not deliver what is asked: too little I or "
                        "feed-forward, or the actuators cannot keep up.",
                        recommendation=(
                            f"Raise the {r.axis} rate I gain (or feed-forward on fixed wing) "
                            "gradually; check that motors or servos are not at their limit."
                        ),
                        measured=r.final,
                        threshold=config["final_low"],
                        comparison="<",
                        unit="×",
                        evidence_signals=evidence,
                    )
                )
            if sluggish and r.rise_s is not None:
                findings.append(
                    Finding(
                        self.id,
                        Severity.ATTENTION,
                        f"{name} rate response is sluggish",
                        f"The {r.axis} rate takes {r.rise_s * 1000:.0f} ms to reach 90% of its "
                        f"final value. {basis} A slow loop lets disturbances through before it "
                        "corrects them.",
                        recommendation=(
                            f"Raise the {r.axis} rate P gain (or feed-forward) a step at a time, "
                            "watching for overshoot."
                        ),
                        measured=r.rise_s * 1000,
                        threshold=rise_limit * 1000,
                        unit="ms",
                        evidence_signals=evidence,
                    )
                )
        if not findings:
            summary = "; ".join(
                f"{r.axis}: peak {r.peak:.2f}×, settles at {r.final:.2f}×, "
                f"rise {r.rise_s * 1000:.0f} ms"
                if r.rise_s is not None
                else f"{r.axis}: peak {r.peak:.2f}×"
                for r in judged
            )
            findings.append(
                Finding(
                    self.id,
                    Severity.OK,
                    "Rate loops well tuned",
                    f"Step response {summary}."
                    + (f" Not judged: {'; '.join(skipped)}." if skipped else ""),
                    evidence_signals=("rate.des_roll", "rate.roll"),
                )
            )
        return findings
