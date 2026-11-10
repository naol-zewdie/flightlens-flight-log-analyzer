from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from flightlens.checks import Check, all_checks
from flightlens.engine import Verdict, analyze, health_score
from flightlens.model import CheckConfig, Finding, Flight, FlightMetadata, Severity


@dataclass
class FixedCheck:
    """A check that returns preset severities, for testing the engine."""

    id: str
    severities: list[Severity]
    required_signals: tuple[str, ...] = ()
    title: str = "Fixed"
    version: str = "0"
    defaults: dict[str, float] = field(default_factory=lambda: {"limit": 1.0})

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        return [Finding(self.id, s, f"{self.id} {s}", "") for s in self.severities]


class BrokenCheck(FixedCheck):
    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        raise RuntimeError("boom")


def empty_flight() -> Flight:
    return Flight(metadata=FlightMetadata(log_format="synthetic"))


def f(sev: Severity) -> Finding:
    return Finding("x", sev, "t", "e")


@pytest.mark.parametrize(
    ("severities", "score"),
    [
        ([], 100),
        ([Severity.OK, Severity.NOT_EVALUATED, Severity.ERROR], 100),
        ([Severity.ATTENTION], 90),
        ([Severity.PROBLEM, Severity.ATTENTION, Severity.ATTENTION], 50),
        ([Severity.PROBLEM] * 4, 0),
    ],
)
def test_health_score(severities: list[Severity], score: int) -> None:
    assert health_score(f(s) for s in severities) == score


def test_verdict_and_exit_code() -> None:
    report = analyze(
        empty_flight(),
        checks=[FixedCheck("a", [Severity.ATTENTION]), FixedCheck("b", [Severity.PROBLEM])],
    )
    assert report.verdict is Verdict.PROBLEM
    assert report.verdict.exit_code == 2
    assert [x.severity for x in report.findings] == [Severity.PROBLEM, Severity.ATTENTION]


def test_missing_signal_is_not_evaluated_never_ok() -> None:
    check = FixedCheck("needs", [Severity.OK], required_signals=("bat.volt",))
    report = analyze(empty_flight(), checks=[check])
    [result] = report.results
    assert result.status is Severity.NOT_EVALUATED
    assert report.incomplete == [result]


def test_broken_check_does_not_stop_others() -> None:
    report = analyze(
        empty_flight(),
        checks=[BrokenCheck("broken", []), FixedCheck("fine", [Severity.ATTENTION])],
    )
    statuses = {r.check_id: r.status for r in report.results}
    assert statuses == {"broken": Severity.ERROR, "fine": Severity.ATTENTION}
    assert "boom" in report.results[0].findings[0].explanation


def test_unknown_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown check"):
        analyze(empty_flight(), checks=[FixedCheck("a", [])], config={"typo": {}})
    with pytest.raises(ValueError, match="unknown setting"):
        analyze(empty_flight(), checks=[FixedCheck("a", [])], config={"a": {"limt": 2.0}})


def test_builtin_checks_are_discovered_and_valid() -> None:
    checks = all_checks()
    assert "vibration.level" in {c.id for c in checks}
    for c in checks:
        assert isinstance(c, Check)
        assert c.version and c.defaults
        assert c.required_signals or c.id == "events.failsafes"  # events need no signals
        assert c.__doc__ or type(c).__module__, c.id


def test_report_json_carries_provenance() -> None:
    report = analyze(empty_flight(), checks=[FixedCheck("a", [Severity.OK])])
    data = report.to_dict()
    assert data["flightlens_version"]
    assert data["checks"][0] == {"id": "a", "title": "Fixed", "version": "0", "status": "ok"}
