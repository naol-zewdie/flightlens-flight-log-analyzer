"""Flight outcome checks: crash or hard landing, and takeoffs that never happened.

Crash (``flight.crash``). An impact is a short burst of extreme vibration
(ArduPilot VIBE above 60 m/s² for at most 1.5 s). An impact during or right
after a flight is a crash when any of these is true:

- the attitude was upset (|roll| > 90° or |pitch| > 60°) in the 10 s before it,
- the aircraft came to rest on its side or inverted (|roll| > 60° after it),
- the log ends, still armed, within 10 s of it (power cut by the impact),
- the firmware's own crash detector fired around it.

Otherwise it is a hard landing (Attention). An attitude upset that is followed
by the end of the log while armed is a crash even without vibration data.
The engine lists the other findings in the 20 s before a crash as likely
contributing factors.

Takeoff (``flight.takeoff``). An armed period in which a takeoff was commanded
(a Takeoff/Auto mode, or throttle or motor output above half for 3 s) but the
altitude never changed by 1 m: the aircraft did not take off.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import (
    IMPACT_VIBE,
    airframe,
    climbed,
    held_windows,
    impact_windows,
    in_air_mask,
    motor_signals,
)
from flightlens.checks.base import register
from flightlens.intervals import format_time, intervals_where
from flightlens.model import (
    CheckConfig,
    EventKind,
    Finding,
    Flight,
    Severity,
    Signal,
)

TAKEOFF_MODES = ("takeoff", "auto", "mission", "vtol takeoff")


@register
class FlightCrash:
    id = "flight.crash"
    title = "Crash or hard landing"
    version = "1.1.0"
    required_signals = ("att.roll", "att.pitch")
    defaults = {
        "upset_roll_deg": 90.0,
        "upset_pitch_deg": 60.0,
        "rest_roll_deg": 60.0,
        "lookback_s": 10.0,
        "end_after_impact_s": 10.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        roll, pitch = flight.signal("att.roll"), flight.signal("att.pitch")
        t = roll.t
        air = in_air_mask(flight, t)
        if not air.any():
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Crash check not evaluated",
                    "The aircraft never left the ground in this log.",
                    evidence_signals=("att.roll",),
                )
            ]
        p = np.interp(t, pitch.t, pitch.values)
        upset = air & (
            (np.abs(roll.values) > config["upset_roll_deg"])
            | (np.abs(p) > config["upset_pitch_deg"])
        )
        log_end = flight.metadata.log_duration or float(t[-1])
        ends_armed = any(e.text == "Log ends while armed" for e in flight.events)
        fw_crash = [e.t for e in flight.events if e.text == "Firmware crash detector"]
        flights = intervals_where(t, air, merge_gap=5.0)

        findings: list[Finding] = []
        used: set[int] = set()
        for a, b in impact_windows(flight, pad_s=0.0):
            # Only impacts during a flight or within 3 s after it ended.
            k = next((i for i, (fa, fb) in enumerate(flights) if fa - 1 <= a <= fb + 3), None)
            if k is None or k in used:
                continue
            look = (t >= a - config["lookback_s"]) & (t <= b)
            after = (t > b + 0.5) & (t <= b + 4.0)
            reasons = []
            if (upset & look).any():
                i = int(np.flatnonzero(upset & look)[0])
                reasons.append(
                    f"the attitude was upset first (roll {roll.values[i]:.0f}°, "
                    f"pitch {p[i]:.0f}° at {format_time(float(t[i]))})"
                )
            if after.any() and np.median(np.abs(roll.values[after])) > config["rest_roll_deg"]:
                reasons.append(
                    f"it came to rest at {np.median(roll.values[after]):.0f}° of roll "
                    "(on its side or inverted)"
                )
            if ends_armed and log_end - b <= config["end_after_impact_s"]:
                reasons.append(f"the log ends {max(log_end - b, 0):.0f} s later, still armed")
            if any(a - 5 <= ft <= b + 5 for ft in fw_crash):
                reasons.append("the flight controller's crash detector fired")
            peak = _peak_vibe(flight, a, b)
            used.add(k)
            if reasons:
                start = _upset_start(t, upset & look) or a
                findings.append(
                    Finding(
                        self.id,
                        Severity.PROBLEM,
                        "Crash",
                        f"Impact at {format_time(a)} ({peak:.0f} m/s² vibration): "
                        + "; ".join(reasons)
                        + ".",
                        recommendation=(
                            "Inspect the airframe before flying again. Review what happened in "
                            "the seconds before the impact (the findings listed with it)."
                        ),
                        measured=peak,
                        threshold=IMPACT_VIBE,
                        unit="m/s²",
                        start=start,
                        end=b,
                        evidence_signals=_evidence(flight),
                    )
                )
            elif abs(flights[k][1] - a) <= 3.0:
                findings.append(
                    Finding(
                        self.id,
                        Severity.ATTENTION,
                        "Hard landing",
                        f"Touchdown at {format_time(a)} with a {peak:.0f} m/s² impact; the "
                        "aircraft stayed upright and the flight ended normally.",
                        recommendation="Check the landing gear, mounts and propellers.",
                        measured=peak,
                        threshold=IMPACT_VIBE,
                        unit="m/s²",
                        start=a,
                        end=b,
                        evidence_signals=_evidence(flight),
                    )
                )

        # An upset followed by the end of the log, without vibration data.
        if not findings and ends_armed and upset.any():
            last = float(t[np.flatnonzero(upset)[-1]])
            if log_end - last <= config["end_after_impact_s"]:
                findings.append(
                    Finding(
                        self.id,
                        Severity.PROBLEM,
                        "Crash",
                        f"The attitude was upset at {format_time(last)} and the log ends "
                        f"{log_end - last:.0f} s later while still armed.",
                        recommendation="Inspect the airframe before flying again.",
                        start=_upset_start(t, upset) or last,
                        end=log_end,
                        evidence_signals=_evidence(flight),
                    )
                )
        return findings


def _peak_vibe(flight: Flight, a: float, b: float) -> float:
    peak = 0.0
    for axis in "xyz":
        s = flight.signals.get(f"vibe.{axis}")
        if s is not None:
            sel = (s.t >= a) & (s.t <= b)
            if sel.any():
                peak = max(peak, float(np.nanmax(s.values[sel])))
    return peak


def _upset_start(t: np.ndarray, mask: np.ndarray) -> float | None:
    idx = np.flatnonzero(mask)
    return float(t[idx[0]]) if idx.size else None


def _evidence(flight: Flight) -> tuple[str, ...]:
    names = ["att.roll", "att.pitch"]
    names += [n for n in ("ctrl.ail", "vibe.z") if n in flight.signals]
    return tuple(names)


@register
class Takeoff:
    id = "flight.takeoff"
    title = "Takeoff"
    version = "1.1.0"
    required_signals = ("pos.rel_alt",)
    defaults = {"command_level": 0.5, "command_s": 3.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        if not flight.segments:
            return []
        findings: list[Finding] = []
        fixed_wing = airframe(flight) == "fixed_wing"
        for seg, flew in zip(flight.segments, climbed(flight), strict=True):
            if flew:
                continue
            why = self._commanded(flight, seg.start, seg.end, config)
            if why is None:
                continue
            sitl = "sitl" in (flight.metadata.hardware or "").lower()
            findings.append(
                Finding(
                    self.id,
                    Severity.ATTENTION if fixed_wing else Severity.PROBLEM,
                    "Did not take off" if fixed_wing else "Takeoff failed",
                    f"{why}, but the altitude never changed by 1 m in the "
                    f"{seg.end - seg.start:.0f} s it was armed.",
                    recommendation=(
                        "Simulator: check that the physics simulator (e.g. Gazebo) is running "
                        "and in sync, and the vehicle model loaded."
                        if sitl
                        else "Check propellers (direction and order), motor order and wiring, "
                        "and that the aircraft is not too heavy for its power."
                        if not fixed_wing
                        else "If this was a takeoff attempt, check airspeed at rotation, "
                        "elevator response and CG."
                    ),
                    start=seg.start,
                    end=seg.end,
                    evidence_signals=tuple(
                        n
                        for n in ("pos.rel_alt", "ctrl.thrust", "ctrl.thr", "mot.1")
                        if n in flight.signals
                    ),
                )
            )
        return findings

    def _commanded(
        self, flight: Flight, start: float, end: float, config: CheckConfig
    ) -> str | None:
        for e in flight.events:
            if (
                e.kind is EventKind.MODE
                and start - 1 <= e.t <= end
                and e.text.lower() in TAKEOFF_MODES
            ):
                return f"{e.text} mode was engaged at {format_time(e.t)}"
        level, dur = config["command_level"], config["command_s"]
        candidates: list[tuple[str, Signal]] = [
            (label, flight.signals[n])
            for label, n in (("Thrust", "ctrl.thrust"), ("Throttle", "ctrl.thr"))
            if n in flight.signals
        ]
        motors = motor_signals(flight)
        if motors and all(m.t.shape == motors[0].t.shape for m in motors):
            mean = np.mean(np.column_stack([m.values for m in motors]), axis=1)
            candidates.append(("Mean motor output", Signal("mot.mean", "", motors[0].t, mean)))
        for label, sig in candidates:
            sel = (sig.t >= start) & (sig.t <= end) & np.isfinite(sig.values)
            if held_windows(sig.t, sel & (sig.values > level), dur):
                peak = float(np.nanmax(sig.values[sel]))
                return f"{label} reached {peak:.0%} for more than {dur:.0f} s"
        return None
