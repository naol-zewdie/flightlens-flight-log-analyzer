"""Runs checks against a flight and turns findings into a verdict.

Health score: start at 100, subtract 10 per Attention and 30 per Problem,
floor at 0. Any Problem makes the verdict Problem; otherwise any Attention
makes it Attention. Checks that could not run are reported, never counted as
passes.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from flightlens import __version__
from flightlens.checks.base import Check, all_checks, alternative_signals
from flightlens.intervals import format_time
from flightlens.model import Finding, Flight, Severity
from flightlens.summary import summarize

if TYPE_CHECKING:
    from flightlens.profiles import Profile

ATTENTION_PENALTY = 10
PROBLEM_PENALTY = 30
MIN_FLIGHT_S = 10.0
"""Armed time below which a log is too short to call the aircraft healthy."""
MIN_CHECKS_FOR_HEALTHY = 3
"""A Healthy verdict needs at least this many checks that actually ran.
One passing check is not evidence that the aircraft is healthy."""
OUTCOME_CHECKS = ("flight.crash",)
"""Findings that describe an outcome; other findings just before them are linked
as likely contributing factors."""
CAUSE_WINDOW_S = 20.0
"""At least this far back; normally back to the takeoff of the same sortie."""
FIRST_IN_SEVERITY = ("flight.crash", "flight.takeoff", "events.failsafes")
"""Within a severity, outcomes and what the flight controller itself flagged come
first: they say what happened, the rest says why."""


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
    profile: Profile | None = None
    """Threshold profile used; None means the checks' defaults."""
    findings: list[Finding] = field(init=False)

    def __post_init__(self) -> None:
        all_findings = _link_causes([f for r in self.results for f in r.findings], self.flight)
        self.findings = sorted(
            all_findings,
            key=lambda f: (
                -f.severity.rank,
                FIRST_IN_SEVERITY.index(f.check_id)
                if f.check_id in FIRST_IN_SEVERITY
                else len(FIRST_IN_SEVERITY),
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
        if self.flight.never_armed:
            # Findings may explain why (arming refused), but there was no flight to judge.
            return Verdict.INSUFFICIENT_DATA
        v = verdict_for(self.findings)
        if v is Verdict.HEALTHY:
            # Time in the air when the log tells it (a taxi or a ground run-up is
            # armed but not a flight to call healthy), else armed time, else the log.
            airborne = summarize(self.flight).airborne_s
            if airborne is not None:
                flown_s: float | None = airborne
            elif self.flight.arming_known:
                flown_s = sum(s.duration for s in self.flight.segments)
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
                "serial": md.serial,
                "start_time": md.start_time.isoformat() if md.start_time else None,
                "log_duration_s": md.log_duration,
                "mapping_version": md.mapping_version,
            },
            "profile": self.profile.provenance() if self.profile else None,
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


def _sortie_start(flight: Flight, t: float) -> float:
    """Takeoff time of the sortie that contains ``t`` (or its armed period's start)."""
    from flightlens.checks._util import takeoff_times

    starts = [s.start for s in flight.segments if s.start <= t]
    starts += [x for x in takeoff_times(flight) if x <= t]
    return max(starts, default=0.0)


def _link_causes(findings: list[Finding], flight: Flight) -> list[Finding]:
    """Attach what went wrong earlier in the same sortie to an outcome (a crash).

    Timed findings between the sortie's takeoff and the outcome are listed in
    order; findings without a time window (e.g. out of trim) are standing
    conditions of the whole flight and are listed too.
    """
    issues = [
        f
        for f in findings
        if f.severity in (Severity.ATTENTION, Severity.PROBLEM) and f.check_id not in OUTCOME_CHECKS
    ]
    out = []
    for f in findings:
        if f.check_id in OUTCOME_CHECKS and f.severity is Severity.PROBLEM and f.start is not None:
            end = f.end if f.end is not None else f.start
            since = min(_sortie_start(flight, f.start), f.start - CAUSE_WINDOW_S)
            timed = sorted(
                (
                    c
                    for c in issues
                    if c.start is not None
                    and c.start <= end
                    and (c.end if c.end is not None else c.start) >= since
                ),
                key=lambda c: c.start or 0.0,
            )
            standing = [c for c in issues if c.start is None]
            parts = []
            if timed:
                listed = "; ".join(f"{c.title} at {format_time(c.start or 0.0)}" for c in timed)
                parts.append(f"Earlier in this flight: {listed}.")
            if standing:
                parts.append(f"Throughout: {'; '.join(c.title for c in standing)}.")
            if parts:
                f = dataclasses.replace(
                    f,
                    explanation=f"{f.explanation} {' '.join(parts)}",
                    related=tuple(c.title for c in timed + standing),
                )
        out.append(f)
    return out


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
    profile: Profile | None = None,
) -> Report:
    """Run every applicable check and build a report.

    ``config`` maps a check id to threshold overrides, e.g.
    ``{"vibration.level": {"attention": 25.0}}``. A ``profile`` supplies
    overrides too; ``config`` wins where both set the same value. The profile is
    recorded in the report.
    """
    checks = list(checks) if checks is not None else all_checks()
    config = dict(config or {})
    if profile is not None:
        known_ids = {c.id for c in checks}
        for check_id, values in profile.overrides.items():
            if check_id in known_ids:  # a subset of checks ignores the rest
                config[check_id] = {**values, **config.get(check_id, {})}

    known = {c.id for c in checks}
    unknown = set(config) - known
    if unknown:
        raise ValueError(f"config refers to unknown check(s): {', '.join(sorted(unknown))}")

    return Report(
        flight=flight,
        results=[_run_one(c, flight, config.get(c.id, {})) for c in checks],
        profile=profile,
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
                # a check may say what is missing in plain words, and how to log it
                explanation=getattr(check, "missing_explanation", None)
                or f"The log has no {', '.join(missing)}.",
                recommendation=str(getattr(check, "missing_recommendation", "")),
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
