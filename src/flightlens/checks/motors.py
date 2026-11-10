"""Motor checks: saturation and imbalance.

Signals: lift motors ``mot.1..N`` as fractions of full output (0-1). VTOL
pushers are excluded by the parser.

Saturation: total time any lift motor spends above 95% while armed.
Attention > 1 s, Problem > 5 s. A saturated motor leaves no headroom for
control, so attitude can no longer be held.

Imbalance: in steady hover (armed, motors spinning, angular rates under
10 deg/s, climb under 0.5 m/s), the spread of each motor's mean output around
the average. Attention > 10%, Problem > 20%. Needs at least 10 s of hover.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import in_air_mask, motor_signals, resample
from flightlens.checks.base import register
from flightlens.intervals import intervals_where
from flightlens.model import CheckConfig, Finding, Flight, Severity


def _motor_matrix(flight: Flight) -> tuple[np.ndarray, np.ndarray] | None:
    """Time vector and (samples x motors) matrix, when all motors share time stamps."""
    sigs = motor_signals(flight)
    if not sigs or any(s.t.shape != sigs[0].t.shape for s in sigs):
        return None
    return sigs[0].t, np.column_stack([s.values for s in sigs])


@register
class MotorSaturation:
    id = "motor.saturation"
    title = "Motor saturation"
    version = "1.0.0"
    required_signals = ("mot.1", "mot.2")
    defaults = {"limit": 0.95, "attention_s": 1.0, "problem_s": 5.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        m = _motor_matrix(flight)
        if m is None:
            return [_not_evaluated(self.id, "Motor outputs are missing or misaligned.")]
        t, out = m
        armed = flight.armed_mask(t)
        if armed.sum() < 10:
            return [_not_evaluated(self.id, "Too few motor samples while armed.")]

        dt = float(np.median(np.diff(t))) if t.size > 1 else 0.0
        sat = armed & np.any(out > config["limit"], axis=1)
        total = float(sat.sum()) * dt

        if total > config["problem_s"]:
            severity, lim = Severity.PROBLEM, config["problem_s"]
        elif total > config["attention_s"]:
            severity, lim = Severity.ATTENTION, config["attention_s"]
        else:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Motors have headroom",
                    f"Motors spent {total:.1f} s above {config['limit']:.0%} of full output.",
                    measured=total,
                    threshold=config["attention_s"],
                    unit="s",
                    evidence_signals=tuple(f"mot.{i + 1}" for i in range(out.shape[1])),
                )
            ]

        counts = sat[:, None] & (out > config["limit"])
        worst = int(np.argmax(counts.sum(axis=0)))
        windows = intervals_where(t, sat, merge_gap=1.0)
        start, end = max(windows, key=lambda w: w[1] - w[0])
        return [
            Finding(
                check_id=self.id,
                severity=severity,
                title=f"Motor {worst + 1} at full power",
                explanation=(
                    f"Lift motors were above {config['limit']:.0%} output for {total:.1f} s in "
                    f"total, mostly motor {worst + 1}. The controller had no headroom left."
                ),
                recommendation=(
                    "The aircraft may be underpowered or overweight: check the battery, "
                    "propellers and payload, and look for a failing motor or ESC."
                ),
                measured=total,
                threshold=lim,
                unit="s",
                start=start,
                end=end,
                evidence_signals=tuple(f"mot.{i + 1}" for i in range(out.shape[1])),
            )
        ]


@register
class MotorImbalance:
    id = "motor.imbalance"
    title = "Motor balance"
    version = "1.0.0"
    required_signals = ("mot.1", "mot.2", "mot.3", "rate.roll", "rate.pitch", "rate.yaw")
    defaults = {
        "attention": 0.10,
        "problem": 0.20,
        "max_rate_dps": 10.0,
        "max_climb_m_s": 0.5,
        "min_output": 0.1,
        "min_hover_s": 10.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        m = _motor_matrix(flight)
        if m is None:
            return [_not_evaluated(self.id, "Motor outputs are missing or misaligned.")]
        t, out = m

        hover = in_air_mask(flight, t) & np.all(np.isfinite(out), axis=1)
        hover &= out.mean(axis=1) > config["min_output"]
        for axis in ("roll", "pitch", "yaw"):
            rate = resample(flight.signal(f"rate.{axis}"), t)
            hover &= np.abs(rate) < config["max_rate_dps"]
        if "pos.climb" in flight.signals:
            climb = resample(flight.signal("pos.climb"), t, max_gap=1.0)
            hover &= np.abs(climb) < config["max_climb_m_s"]

        dt = float(np.median(np.diff(t))) if t.size > 1 else 0.0
        hover_s = float(hover.sum()) * dt
        if hover_s < config["min_hover_s"]:
            return [
                _not_evaluated(
                    self.id,
                    f"Only {hover_s:.0f} s of steady hover; at least "
                    f"{config['min_hover_s']:.0f} s is needed to compare motors.",
                )
            ]

        means = out[hover].mean(axis=0)
        avg = float(means.mean())
        dev = means / avg - 1.0
        worst = int(np.argmax(np.abs(dev)))
        spread = float(np.abs(dev).max())
        evidence = tuple(f"mot.{i + 1}" for i in range(out.shape[1]))
        harder = "harder" if dev[worst] > 0 else "less"
        headline = f"Motor {worst + 1} works {harder} than the others"
        detail = (
            f"In {hover_s:.0f} s of steady hover, motor {worst + 1} averaged "
            f"{dev[worst]:+.0%} versus the mean of all {out.shape[1]} motors."
        )

        if spread > config["problem"]:
            severity, lim = Severity.PROBLEM, config["problem"]
        elif spread > config["attention"]:
            severity, lim = Severity.ATTENTION, config["attention"]
        else:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Motors balanced",
                    detail,
                    measured=spread * 100,
                    threshold=config["attention"] * 100,
                    unit="%",
                    evidence_signals=evidence,
                )
            ]
        hover_windows = intervals_where(t, hover, merge_gap=1.0)
        start, end = max(hover_windows, key=lambda w: w[1] - w[0])
        return [
            Finding(
                check_id=self.id,
                severity=severity,
                title=headline,
                explanation=detail,
                recommendation=(
                    "Check the centre of gravity, look for a twisted motor mount or bent arm, "
                    "and test that motor and its ESC."
                ),
                measured=spread * 100,
                threshold=lim * 100,
                unit="%",
                start=start,
                end=end,
                evidence_signals=evidence,
            )
        ]


def _not_evaluated(check_id: str, why: str) -> Finding:
    title = "Motor saturation" if check_id == "motor.saturation" else "Motor balance"
    return Finding(check_id, Severity.NOT_EVALUATED, f"{title} not evaluated", why)
