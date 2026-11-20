"""Control checks: attitude beyond the configured limits, control surfaces at full
travel, and an aircraft out of trim.

Attitude limits (``control.attitude_limits``). Roll or pitch beyond the limit
the aircraft is configured for, held 1 s in the air. Limits come from the
log's parameters: ArduPlane ROLL_LIMIT_DEG / LIM_ROLL_CD and PTCH_LIM_* /
LIM_PITCH_*; PX4 fixed wing FW_R_LIM, FW_P_LIM_*; ArduCopter ANGLE_MAX; PX4
multicopter MPC_TILTMAX_AIR. Attention beyond the limit; Problem beyond the
limit + 30° or past 150° (inverted).

Saturation (``control.saturation``). Aileron or elevator at 95% or more of its
travel in the air. Attention above 1 s in total, Problem above 5 s: the pilot
or controller had no authority left on that axis.

Trim (``control.trim``). In steady wings-level flight (|roll| < 10° for at
least 10 s), the median aileron deflection. A straight aircraft needs almost
none; Attention above 10% of travel, Problem above 25%.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import (
    held_windows,
    impact_windows,
    in_air_mask,
    outside,
    resample,
)
from flightlens.checks.base import register
from flightlens.intervals import format_time, intervals_where
from flightlens.model import CheckConfig, EventKind, Finding, Flight, Severity


def _limit(params: dict[str, float], deg: tuple[str, ...], centi: tuple[str, ...]) -> float | None:
    for p in deg:
        if params.get(p, 0) > 0:
            return float(params[p])
    for p in centi:
        if params.get(p, 0) > 0:
            return float(params[p]) / 100.0
    return None


def _mode_at(flight: Flight, t: float) -> str | None:
    modes = [e for e in flight.events if e.kind is EventKind.MODE and e.t <= t]
    return modes[-1].text if modes else None


@register
class AttitudeLimits:
    id = "control.attitude_limits"
    title = "Attitude limits"
    version = "1.0.0"
    required_signals = ("att.roll",)
    defaults = {"min_duration_s": 1.0, "problem_margin_deg": 30.0, "inverted_deg": 150.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        params = flight.parameters
        limits = {
            "roll": _limit(
                params,
                ("ROLL_LIMIT_DEG", "FW_R_LIM", "MPC_TILTMAX_AIR"),
                ("LIM_ROLL_CD", "ANGLE_MAX"),
            ),
            "pitch": _limit(params, ("PTCH_LIM_MAX_DEG", "FW_P_LIM_MAX"), ("LIM_PITCH_MAX",)),
        }
        if limits["roll"] is None:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Attitude limits not evaluated",
                    "The log has no roll limit parameter to compare against.",
                    evidence_signals=("att.roll",),
                )
            ]
        findings: list[Finding] = []
        for axis, lim in limits.items():
            if lim is None or f"att.{axis}" not in flight.signals:
                continue
            sig = flight.signal(f"att.{axis}")
            usable = (
                in_air_mask(flight, sig.t)
                & outside(sig.t, impact_windows(flight, pad_s=0.5))
                & np.isfinite(sig.values)
            )
            if usable.sum() < 10:
                continue
            mag = np.abs(sig.values)
            hard = lim + config["problem_margin_deg"]
            severity = None
            for sev, level, dur in (
                (Severity.PROBLEM, min(hard, config["inverted_deg"]), 0.5),
                (Severity.ATTENTION, lim, config["min_duration_s"]),
            ):
                wins = held_windows(sig.t, usable & (mag > level), dur, merge_gap=0.3)
                if wins:
                    severity = sev
                    break
            if severity is None:
                continue
            dt = float(np.median(np.diff(sig.t))) if sig.t.size > 1 else 0.0
            total = float((usable & (mag > lim)).sum()) * dt
            a, b = max(wins, key=lambda w: float(mag[(sig.t >= w[0]) & (sig.t <= w[1])].max()))
            sel = (sig.t >= a) & (sig.t <= b)
            peak = float(sig.values[sel][np.argmax(mag[sel])])
            mode = _mode_at(flight, a)
            word = "Bank" if axis == "roll" else "Pitch"
            findings.append(
                Finding(
                    self.id,
                    severity,
                    f"{word} angle beyond the {lim:.0f}° limit",
                    f"{word} reached {peak:.0f}° against a configured limit of {lim:.0f}°, "
                    f"and was beyond the limit for {total:.0f} s in total"
                    + (
                        f" (mode {mode}"
                        + (
                            ", not limited by the autopilot)"
                            if mode and mode.lower() in ("manual", "acro", "training")
                            else ")"
                        )
                        if mode
                        else ""
                    )
                    + ".",
                    recommendation=(
                        "If the pilot did not command it, the aircraft was not under control on "
                        "this axis: review control-surface throws, CG and the attitude tuning."
                    ),
                    measured=abs(peak),
                    threshold=lim,
                    unit="°",
                    start=a,
                    end=b,
                    evidence_signals=(f"att.{axis}",)
                    + (("ctrl.ail",) if axis == "roll" and "ctrl.ail" in flight.signals else ()),
                )
            )
        return findings


@register
class ControlSaturation:
    id = "control.saturation"
    title = "Control surface travel"
    version = "1.0.0"
    required_signals = ("ctrl.ail",)
    alternative_signals = (("ctrl.elev",),)
    defaults = {"limit": 0.95, "attention_s": 1.0, "problem_s": 5.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        findings: list[Finding] = []
        evaluated = False
        for name, label in (("ctrl.ail", "Aileron"), ("ctrl.elev", "Elevator")):
            sig = flight.signals.get(name)
            if sig is None:
                continue
            usable = (
                in_air_mask(flight, sig.t)
                & outside(sig.t, impact_windows(flight, pad_s=0.5))
                & np.isfinite(sig.values)
            )
            if usable.sum() < 10:
                continue
            evaluated = True
            sat = usable & (np.abs(sig.values) >= config["limit"])
            dt = float(np.median(np.diff(sig.t))) if sig.t.size > 1 else 0.0
            total = float(sat.sum()) * dt
            if total <= config["attention_s"]:
                continue
            severity = Severity.PROBLEM if total > config["problem_s"] else Severity.ATTENTION
            wins = intervals_where(sig.t, sat, merge_gap=0.3)
            a, b = max(wins, key=lambda w: w[1] - w[0])
            air_s = float(usable.sum()) * dt
            findings.append(
                Finding(
                    self.id,
                    severity,
                    f"{label} at full travel",
                    f"The {label.lower()} was at {config['limit']:.0%} or more of its travel for "
                    f"{total:.1f} s ({total / air_s:.0%} of the time in the air), longest "
                    f"{b - a:.1f} s at {format_time(a)}. With no travel left, the attitude on "
                    "this axis could not be corrected.",
                    recommendation=(
                        "Check control-surface size and servo throws, the CG, and the "
                        "aircraft's trim; in stabilized modes review the attitude gains."
                    ),
                    measured=total,
                    threshold=config["problem_s"]
                    if severity is Severity.PROBLEM
                    else config["attention_s"],
                    unit="s",
                    start=a,
                    end=b,
                    evidence_signals=(name, "att.roll" if name == "ctrl.ail" else "att.pitch"),
                )
            )
        if not evaluated:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Control surface travel not evaluated",
                    "Too few control-surface samples in the air.",
                    evidence_signals=("ctrl.ail",),
                )
            ]
        return findings


@register
class Trim:
    id = "control.trim"
    title = "Trim"
    version = "1.0.0"
    required_signals = ("ctrl.ail", "att.roll")
    defaults = {
        "attention": 0.10,
        "problem": 0.25,
        "level_deg": 10.0,
        "min_level_s": 10.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        ail = flight.signal("ctrl.ail")
        roll = resample(flight.signal("att.roll"), ail.t)
        level = (
            in_air_mask(flight, ail.t)
            & outside(ail.t, impact_windows(flight))
            & (np.abs(roll) < config["level_deg"])
            & np.isfinite(ail.values)
        )
        dt = float(np.median(np.diff(ail.t))) if ail.t.size > 1 else 0.0
        level_s = float(level.sum()) * dt
        if level_s < config["min_level_s"]:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Trim not evaluated",
                    f"Only {level_s:.0f} s of wings-level flight; at least "
                    f"{config['min_level_s']:.0f} s is needed.",
                    evidence_signals=("ctrl.ail", "att.roll"),
                )
            ]
        offset = float(np.median(ail.values[level]))
        if abs(offset) <= config["attention"]:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Aircraft in trim",
                    f"Wings-level flight needed {abs(offset):.0%} aileron on average.",
                    measured=abs(offset) * 100,
                    threshold=config["attention"] * 100,
                    unit="%",
                    evidence_signals=("ctrl.ail", "att.roll"),
                )
            ]
        severity = Severity.PROBLEM if abs(offset) > config["problem"] else Severity.ATTENTION
        side = "left" if offset < 0 else "right"
        drift = "right" if side == "left" else "left"
        limit = config["problem"] if severity is Severity.PROBLEM else config["attention"]
        return [
            Finding(
                self.id,
                severity,
                "Aircraft out of trim (roll)",
                f"To fly wings-level the aircraft needed {abs(offset):.0%} {side} aileron on "
                f"average ({level_s:.0f} s of level flight). It rolls {drift} by itself, which "
                "uses up roll authority and makes it hard to control.",
                recommendation=(
                    "Check aileron servo centring (SERVOn_TRIM), linkages and a warped wing or "
                    "bent surface; check the lateral CG and motor thrust line."
                ),
                measured=abs(offset) * 100,
                threshold=limit * 100,
                unit="%",
                evidence_signals=("ctrl.ail", "att.roll"),
            )
        ]
