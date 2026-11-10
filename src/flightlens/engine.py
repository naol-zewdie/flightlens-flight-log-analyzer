"""Runs checks against a flight and turns findings into a verdict.

Health score: start at 100, subtract 10 per Attention and 30 per Problem,
floor at 0. Any Problem makes the verdict Problem; otherwise any Attention
makes it Attention. Checks that could not run are reported, never counted as
passes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from flightlens import __version__
from flightlens.checks.base import Check, all_checks, alternative_signals
from flightlens.model import Finding, Flight, Severity
from flightlens.summary import summarize

ATTENTION_PENALTY = 10
PROBLEM_PENALTY = 30
MIN_FLIGHT_S = 10.0
"""Armed time below which a log is too short to call the aircraft healthy."""
MIN_CHECKS_FOR_HEALTHY = 3
"""A Healthy verdict needs at least this many checks that actually ran.
One passing check is not evidence that the aircraft is healthy."""


class Verdict(StrEnum):
    HEALTHY = "healthy"
    ATTENTION = "attention"
    PROBLEM = "problem"
    INSUFFICIENT_DATA = "insufficient data"
    """No check could run, so health cannot be judged. Never shown as healthy."""

    @property
    def exit_code(self) -> int:
        return {
            Verdict.HEALTHY: 0,
            Verdict.ATTENTION: 1,
            Verdict.PROBLEM: 2,
            Verdict.INSUFFICIENT_DATA: 3,
        }[self]


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    version: str
    status: Severity
    findings: list[Finding]


@dataclass
class Report:
    flight: Flight
    results: list[CheckResult]
    engine_version: str = __version__
    findings: list[Finding] = field(init=False)

    def __post_init__(self) -> None:
        all_findings = [f for r in self.results for f in r.findings]
        self.findings = sorted(
            all_findings,
            key=lambda f: (
                -f.severity.rank,
                # Within a severity, what the flight controller itself flagged
                # (failsafes, failure detector) comes first: it is usually the cause.
                0 if f.check_id == "events.failsafes" else 1,
                f.start if f.start is not None else float("inf"),
            ),
        )

    @property
    def score(self) -> int | None:
        """None when no check could run: a number would suggest a judgement."""
        if self.verdict is Verdict.INSUFFICIENT_DATA:
            return None
        return health_score(self.findings)

    @property
    def verdict(self) -> Verdict:
        v = verdict_for(self.findings)
        if v is Verdict.HEALTHY:
            # Armed time if the log records arming, otherwise the whole log.
            if self.flight.arming_known:
                flown_s: float | None = sum(s.duration for s in self.flight.segments)
            else:
                flown_s = self.flight.metadata.log_duration
            if flown_s is None or flown_s < MIN_FLIGHT_S:
                return Verdict.INSUFFICIENT_DATA
        return v

    @property
    def incomplete(self) -> list[CheckResult]:
        """Checks that did not produce a real result."""
        return [r for r in self.results if r.status in (Severity.NOT_EVALUATED, Severity.ERROR)]

    def to_dict(self) -> dict[str, Any]:
        md = self.flight.metadata
        return {
            "flightlens_version": self.engine_version,
            "source": {
                "path": str(md.source_path) if md.source_path else None,
                "sha256": md.sha256,
                "format": md.log_format,
                "vehicle_type": md.vehicle_type,
                "firmware": md.firmware,
                "hardware": md.hardware,
                "start_time": md.start_time.isoformat() if md.start_time else None,
                "log_duration_s": md.log_duration,
                "mapping_version": md.mapping_version,
            },
            "verdict": self.verdict.value,
            "score": self.score,
            "summary": summarize(self.flight).to_dict(),
            "data_quality": [
                {"kind": q.kind, "message": q.message, "t": q.t} for q in self.flight.quality
            ],
            "checks": [
                {
                    "id": r.check_id,
                    "title": r.title,
                    "version": r.version,
                    "status": r.status.value,
                }
                for r in self.results
            ],
            "findings": [f.to_dict() for f in self.findings],
        }


def health_score(findings: Iterable[Finding]) -> int:
    score = 100
    for f in findings:
        if f.severity is Severity.PROBLEM:
            score -= PROBLEM_PENALTY
        elif f.severity is Severity.ATTENTION:
            score -= ATTENTION_PENALTY
    return max(score, 0)


def verdict_for(findings: Iterable[Finding]) -> Verdict:
    findings = list(findings)
    severities = {f.severity for f in findings}
    if Severity.PROBLEM in severities:
        return Verdict.PROBLEM
    if Severity.ATTENTION in severities:
        return Verdict.ATTENTION
    evaluated = {f.check_id for f in findings if f.severity is Severity.OK}
    if len(evaluated) >= MIN_CHECKS_FOR_HEALTHY:
        return Verdict.HEALTHY
    return Verdict.INSUFFICIENT_DATA


def analyze(
    flight: Flight,
    checks: Sequence[Check] | None = None,
    config: Mapping[str, Mapping[str, float]] | None = None,
) -> Report:
    """Run every applicable check and build a report.

    ``config`` maps a check id to threshold overrides, e.g.
    ``{"vibration.level": {"attention": 25.0}}``.
    """
    checks = list(checks) if checks is not None else all_checks()
    config = config or {}

    known = {c.id for c in checks}
    unknown = set(config) - known
    if unknown:
        raise ValueError(f"config refers to unknown check(s): {', '.join(sorted(unknown))}")

    return Report(
        flight=flight, results=[_run_one(c, flight, config.get(c.id, {})) for c in checks]
    )


def _run_one(check: Check, flight: Flight, overrides: Mapping[str, float]) -> CheckResult:
    bad_keys = set(overrides) - set(check.defaults)
    if bad_keys:
        raise ValueError(f"{check.id}: unknown setting(s) {', '.join(sorted(bad_keys))}")
    cfg = {**check.defaults, **overrides}

    missing = [s for s in check.required_signals if s not in flight.signals]
    if missing and any(flight.has(*alt) for alt in alternative_signals(check)):
        missing = []
    if missing:
        findings = [
            Finding(
                check_id=check.id,
                severity=Severity.NOT_EVALUATED,
                title=f"{check.title} not evaluated",
                explanation=f"The log has no {', '.join(missing)}.",
                evidence_signals=tuple(missing),
            )
        ]
    else:
        try:
            findings = check.run(flight, cfg)
        except Exception as exc:  # one broken check must not stop the report
            findings = [
                Finding(
                    check_id=check.id,
                    severity=Severity.ERROR,
                    title=f"{check.title} failed",
                    explanation=f"The check raised {type(exc).__name__}: {exc}",
                )
            ]

    if not findings:  # a check that returns nothing found nothing wrong
        findings = [Finding(check.id, Severity.OK, check.title, "No issues found.")]
    status = max((f.severity for f in findings), key=lambda s: s.rank)
    return CheckResult(check.id, check.title, check.version, status, findings)
