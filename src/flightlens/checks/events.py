"""Events, errors and failsafes check.

Signals: none; works on the flight's event list (failsafes, failure detector,
logged errors). Only events while armed (or within 2 s of it) count, because
pre-flight check messages on the ground are expected.

Problem: any failsafe or failure-detector trigger, or a critical message.
Attention: any error message, or a log that ends while still armed.
"""

from __future__ import annotations

from flightlens.checks.base import register
from flightlens.model import CheckConfig, Event, EventKind, Finding, Flight, Severity

MARGIN_S = 2.0


@register
class EventsAndFailsafes:
    id = "events.failsafes"
    title = "Errors and failsafes"
    version = "1.0.0"
    required_signals: tuple[str, ...] = ()
    defaults = {"margin_s": MARGIN_S}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        why = None
        if not flight.arming_known:
            why = "The log has no vehicle status, so failsafes cannot be checked."
        elif flight.never_armed:
            why = "The aircraft never armed in this log."
        if why:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Errors and failsafes not evaluated",
                    why,
                )
            ]
        margin = config["margin_s"]

        def in_flight(e: Event) -> bool:
            if not flight.segments:
                return True
            return any(s.start - margin <= e.t <= s.end + margin for s in flight.segments)

        relevant = [e for e in flight.events if in_flight(e)]
        failsafes = [e for e in relevant if e.kind is EventKind.FAILSAFE]
        critical = [e for e in relevant if e.kind is EventKind.ERROR and e.level == "critical"]
        errors = [e for e in relevant if e.kind is EventKind.ERROR and e.level != "critical"]

        findings: list[Finding] = []
        if failsafes or critical:
            group = failsafes + critical
            first = min(group, key=lambda e: e.t)
            names = list(dict.fromkeys(e.text for e in group))
            findings.append(
                Finding(
                    check_id=self.id,
                    severity=Severity.PROBLEM,
                    title=first.text
                    if len(names) == 1
                    else f"{len(group)} failsafe or critical events",
                    explanation="The flight controller reported in flight: "
                    + "; ".join(names[:5])
                    + ("…" if len(names) > 5 else ""),
                    recommendation="Review what triggered each event on the timeline.",
                    measured=float(len(group)),
                    unit="events",
                    start=first.t,
                    end=max(e.t for e in group),
                )
            )
        if errors:
            first = min(errors, key=lambda e: e.t)
            names = list(dict.fromkeys(e.text for e in errors))
            findings.append(
                Finding(
                    check_id=self.id,
                    severity=Severity.ATTENTION,
                    title=first.text if len(names) == 1 else f"{len(errors)} error messages",
                    explanation="In flight: "
                    + "; ".join(names[:5])
                    + ("…" if len(names) > 5 else ""),
                    recommendation="Check the subsystem named in each message.",
                    measured=float(len(errors)),
                    unit="events",
                    start=first.t,
                    end=max(e.t for e in errors),
                )
            )
        if not findings:
            findings.append(
                Finding(
                    self.id,
                    Severity.OK,
                    "No errors or failsafes",
                    "No failsafe, failure-detector or error events in flight.",
                )
            )
        return findings
