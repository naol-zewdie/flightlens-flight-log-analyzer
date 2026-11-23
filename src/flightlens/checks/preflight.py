"""Pre-flight check: why arming was refused.

Signals: none; reads the flight controller's messages. PX4 logs
"Preflight Fail: <reason>" and "Arming denied: ..."; ArduPilot logs
"PreArm: <reason>" and "Arm: <reason>".

If the aircraft never armed, the refusal reasons are the answer to "why did
nothing happen", so they are reported as Attention (the verdict stays
*insufficient data*: there was no flight to judge). If it armed later, the
earlier refusals are summarised in an OK finding for context.
"""

from __future__ import annotations

import re
from collections import Counter

from flightlens.checks.base import register
from flightlens.model import CheckConfig, EventKind, Finding, Flight, Severity

REASON = re.compile(
    r"^(?:\[[^\]]+\]\s*)?(?:Preflight Fail|PreArm|Arm|Arming disabled):\s*(.+)$",
    re.IGNORECASE,
)
DENIED = re.compile(r"arming denied|arming refused|preflight check: failed|^arm: ", re.IGNORECASE)


@register
class ArmingRefused:
    id = "preflight.arming"
    title = "Arming"
    version = "1.1.0"
    required_signals: tuple[str, ...] = ()
    defaults = {"max_reasons": 5.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        first_arm = min((s.start for s in flight.segments), default=None)
        reasons: Counter[str] = Counter()
        denials = 0
        for e in flight.events:
            if e.kind not in (EventKind.MESSAGE, EventKind.ERROR):
                continue
            if first_arm is not None and e.t >= first_arm:
                break
            text = e.text.strip()
            m = REASON.match(text)
            if m:
                reasons[m.group(1).strip().rstrip(".")] += 1
            if DENIED.search(text):
                denials += 1
        if not reasons and not denials:
            if flight.never_armed:
                return [
                    Finding(
                        self.id,
                        Severity.NOT_EVALUATED,
                        "Never armed",
                        "The aircraft never armed and the log records no reason.",
                    )
                ]
            return []

        listed = "; ".join(
            f"{r} ({n}×)" if n > 1 else r
            for r, n in reasons.most_common(int(config["max_reasons"]))
        )
        if flight.never_armed:
            return [
                Finding(
                    self.id,
                    Severity.ATTENTION,
                    "Arming was refused",
                    "The flight controller refused to arm"
                    + (f" {denials}×" if denials > 1 else "")
                    + (f": {listed}." if listed else ", without giving a reason."),
                    recommendation=(
                        "Fix the listed pre-arm failure before flying. In a simulator, "
                        "'height estimate not stable' usually means the simulated sensors are "
                        "not running or not yet settled: wait for the estimator, or restart "
                        "the simulation."
                    ),
                    measured=float(max(denials, 1)),
                    unit="attempts",
                )
            ]
        return [
            Finding(
                self.id,
                Severity.OK,
                "Armed after pre-arm failures",
                f"Arming was refused before it succeeded: {listed or 'no reason given'}.",
            )
        ]
