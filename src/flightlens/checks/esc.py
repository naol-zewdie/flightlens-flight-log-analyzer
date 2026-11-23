"""ESC telemetry: temperatures, and motors that stop while commanded to spin.

**esc.health** — signals ``esc.N.rpm`` and ``esc.N.temp`` (PX4 ``esc_status``,
ArduPilot ``ESC``; BLHeli/AM32 telemetry or bidirectional DShot), in the air.

* *Temperature*: Attention above ``temp_attention`` (90 °C), Problem above
  ``temp_problem`` (110 °C), held ``temp_min_s`` (2 s). In the public logs that
  report it, ESCs peak at 25-63 °C. 0 °C means "not reported" and is ignored.
* *Stopped motor*: on a multirotor, a motor commanded above ``stall_command``
  (30% output) that turns at less than ``stall_ratio`` (20%) of the other
  motors' median RPM, while those turn faster than ``min_rpm_others``
  (1000 rpm), for ``stall_min_s`` (0.5 s): a desync, a failed ESC or a broken
  motor. Short dips (real logs reach 10 rpm while the output is near idle) are
  not judged, because the motor is not being asked to spin.

Telemetry that is logged but carries no RPM and no temperature in the air
(seen on public PX4 logs: every value 0) is *not evaluated*, never OK.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import grade, held_windows, in_air_mask, motor_signals, resample
from flightlens.checks.base import register
from flightlens.intervals import format_time
from flightlens.model import CheckConfig, Finding, Flight, Severity

MAX_ESCS = 8


def _escs(flight: Flight) -> list[int]:
    return [
        i
        for i in range(1, MAX_ESCS + 1)
        if f"esc.{i}.rpm" in flight.signals or f"esc.{i}.temp" in flight.signals
    ]


@register
class EscHealth:
    id = "esc.health"
    title = "ESC telemetry"
    version = "1.0.0"
    required_signals = ("esc.1.rpm",)
    alternative_signals = (("esc.1.temp",),)
    defaults = {
        "temp_attention": 90.0,
        "temp_problem": 110.0,
        "temp_min_s": 2.0,
        "stall_command": 0.3,
        "stall_ratio": 0.2,
        "stall_min_s": 0.5,
        "min_rpm_others": 1000.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        rpm_escs: list[int] = []
        temp_escs: list[int] = []
        for i in _escs(flight):
            for kind, found in (("rpm", rpm_escs), ("temp", temp_escs)):
                sig = flight.signals.get(f"esc.{i}.{kind}")
                if sig is None:
                    continue
                v = sig.values[in_air_mask(flight, sig.t)]
                v = v[np.isfinite(v)]
                if v.size and (kind == "temp" or np.any(np.abs(v) > 0)):
                    found.append(i)
        if not rpm_escs and not temp_escs:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "ESC telemetry not evaluated",
                    "ESC telemetry is missing in the air, or carries no RPM and no temperature "
                    "(every value 0).",
                    evidence_signals=tuple(f"esc.{i}.rpm" for i in _escs(flight)),
                )
            ]

        findings: list[Finding] = []
        findings += self._temperatures(flight, config, temp_escs)
        stall, stall_note = self._stopped(flight, config, rpm_escs)
        findings += stall
        if findings:
            return findings

        count = len(set(rpm_escs) | set(temp_escs))
        parts = [f"{count} ESC{'s' if count != 1 else ''} reported in the air."]
        hottest = None
        for i in temp_escs:
            sig = flight.signal(f"esc.{i}.temp")
            v = sig.values[in_air_mask(flight, sig.t) & np.isfinite(sig.values)]
            hottest = max(hottest or -1e9, float(v.max()))
        if hottest is not None:
            parts.append(f"Hottest {hottest:.0f} °C.")
        parts.append(stall_note)
        return [
            Finding(
                self.id,
                Severity.OK,
                "ESCs healthy",
                " ".join(p for p in parts if p),
                measured=hottest,
                threshold=config["temp_attention"] if hottest is not None else None,
                unit="°C" if hottest is not None else "",
                evidence_signals=tuple(f"esc.{i}.temp" for i in temp_escs)
                or tuple(f"esc.{i}.rpm" for i in rpm_escs),
            )
        ]

    def _temperatures(self, flight: Flight, config: CheckConfig, escs: list[int]) -> list[Finding]:
        worst = None
        for i in escs:
            sig = flight.signal(f"esc.{i}.temp")
            usable = in_air_mask(flight, sig.t) & np.isfinite(sig.values)
            b = grade(
                sig.t,
                sig.values,
                usable,
                config["temp_attention"],
                config["temp_problem"],
                min_duration=config["temp_min_s"],
            )
            if b is not None and (worst is None or b.worst > worst[1].worst):
                worst = (i, b)
        if worst is None:
            return []
        i, b = worst
        return [
            Finding(
                self.id,
                b.severity,
                f"ESC {i} overheating",
                f"ESC {i} reached {b.worst:.0f} °C in the air (at {format_time(b.start)}). "
                "Hot ESCs throttle back or shut down, and the motor stops.",
                recommendation=(
                    "Check the ESC's cooling and airflow, the propeller size for this motor, and "
                    "the motor for binding; consider a higher-rated ESC."
                ),
                measured=b.worst,
                threshold=b.limit,
                unit="°C",
                start=b.start,
                end=b.end,
                evidence_signals=(f"esc.{i}.temp",),
            )
        ]

    def _stopped(
        self, flight: Flight, config: CheckConfig, escs: list[int]
    ) -> tuple[list[Finding], str]:
        motors = {int(s.name.split(".")[1]): s for s in motor_signals(flight)}
        judged = [i for i in escs if i in motors]
        if len(judged) < 2:
            return [], "Motor RPM against the commanded output is only judged on multirotors."
        base = flight.signal(f"esc.{judged[0]}.rpm")
        t = base.t[in_air_mask(flight, base.t)]
        if t.size < 2:
            return [], ""
        rpm = np.vstack([np.abs(resample(flight.signal(f"esc.{i}.rpm"), t, 1.0)) for i in judged])
        worst = None
        for row, i in enumerate(judged):
            others = np.nanmedian(np.delete(rpm, row, axis=0), axis=0)
            cmd = resample(motors[i], t, 1.0)
            with np.errstate(invalid="ignore"):
                bad = (
                    (cmd > config["stall_command"])
                    & (others > config["min_rpm_others"])
                    & (rpm[row] < config["stall_ratio"] * others)
                )
            windows = held_windows(t, bad, config["stall_min_s"])
            if windows:
                a, b = windows[0]
                sel = (t >= a) & (t <= b)
                low = float(np.nanmin(rpm[row][sel]))
                ref = float(np.nanmedian(others[sel]))
                if worst is None or a < worst[1]:
                    worst = (i, a, b, low, ref)
        if worst is None:
            return [], "Every motor turned as commanded."
        i, a, b, low, ref = worst
        return [
            Finding(
                self.id,
                Severity.PROBLEM,
                f"Motor {i} stopped while commanded",
                f"Motor {i} turned at {low:.0f} rpm while commanded to spin and the other "
                f"motors turned at {ref:.0f} rpm ({format_time(a)}-{format_time(b)}): an ESC "
                "desync, a failed ESC or a damaged motor.",
                recommendation=(
                    f"Inspect motor {i}, its ESC and wiring; check the ESC's timing and "
                    "demag settings for desyncs."
                ),
                measured=low,
                threshold=config["stall_ratio"] * ref,
                comparison="<",
                unit="rpm",
                start=a,
                end=b,
                evidence_signals=(f"esc.{i}.rpm", f"mot.{i}"),
            )
        ], ""
