"""The validation benchmark and the scorer behind ``flightlens validate``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from flightlens import validate
from flightlens.cli import app
from flightlens.model import Finding, Severity
from flightlens.sim.benchmark import build, copter


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("bench"))


@pytest.fixture(scope="module")
def outcomes(bench: Path) -> list[validate.Outcome]:
    return [validate.evaluate(c) for c in validate.load_manifest(bench)]


def test_benchmark_is_fully_correct(outcomes: list[validate.Outcome]) -> None:
    failing = {o.case.id: o.problems() for o in outcomes if not o.passed}
    assert not failing, failing
    stats = validate.per_check(outcomes)
    assert all(s.recall == 1.0 and s.false_alarms == 0 for s in stats.values()), stats


def test_benchmark_covers_every_fault_class(outcomes: list[validate.Outcome]) -> None:
    faults = {o.case.fault for o in outcomes}
    assert {"healthy", "healthy (near limit)", "vibration", "battery", "GPS", "motor",
            "crash", "never flew", "failsafe", "estimator"} <= faults  # fmt: skip
    checked = set(validate.per_check(outcomes))
    assert len(checked) >= 15


def test_a_mistuned_threshold_is_caught(bench: Path) -> None:
    """The point of the benchmark: a regression in a check must fail CI."""
    case = next(
        c for c in validate.load_manifest(bench) if c.name == "copter-vibration-problem.bin"
    )
    outcome = validate.evaluate(case, config={"vibration.level": {"problem": 90.0}})
    assert not outcome.passed
    assert outcome.problems() == [
        "verdict attention, expected problem",
        "vibration.level:problem reported as attention",
    ]
    healthy = next(c for c in validate.load_manifest(bench) if c.name == "copter-healthy-0.bin")
    noisy = validate.evaluate(healthy, config={"vibration.level": {"attention": 10.0}})
    assert {f.check_id for f in noisy.false_alarms} == {"vibration.level"}  # one per axis


# --- scorer ------------------------------------------------------------------------


def _finding(
    check: str, sev: Severity, start: float | None = 10, end: float | None = 20
) -> Finding:
    return Finding(check, sev, "t", "e", start=start, end=end)


def test_rule_parsing_and_matching() -> None:
    assert validate.Rule.parse("gps.quality:problem") == validate.Rule("gps.quality", "problem")
    bare = validate.Rule.parse("gps.quality")
    assert bare.matches(_finding("gps.quality", Severity.ATTENTION))
    assert not bare.matches(_finding("gps.quality", Severity.OK))
    windowed = validate.Rule.parse({"check": "x", "window": [30, 40], "tolerance_s": 5})
    assert windowed.matches(_finding("x", Severity.PROBLEM, 34, 50))
    assert windowed.matches(_finding("x", Severity.PROBLEM, 10, 26))  # within tolerance
    assert not windowed.matches(_finding("x", Severity.PROBLEM, 10, 20))
    assert not windowed.matches(_finding("x", Severity.PROBLEM, None, None))
    with pytest.raises(ValueError, match="not a rule"):
        validate.Rule.parse(3)


def test_manifest_defaults_and_relative_dir(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    m = tmp_path / "set.yaml"
    m.write_text(
        yaml.safe_dump(
            {
                "logs_dir": "logs",
                "defaults": {"must_not": ["vibration.level:problem"], "exhaustive": True},
                "logs": {
                    "a.bin": {"must": ["gps.quality"], "must_not": ["imu.clipping"]},
                    "b.bin": None,
                },
            }
        ),
        encoding="utf-8",
    )
    a, b = validate.load_manifest(m)
    assert a.path == tmp_path / "logs" / "a.bin" and a.id == "set/a.bin"
    assert [str(r) for r in a.must_not] == ["vibration.level:problem", "imu.clipping"]
    assert a.exhaustive and [str(r) for r in a.expect] == ["gps.quality"]
    assert validate.evaluate(b).error == "log file not found"


def test_cli_validate_exit_codes_and_reports(tmp_path: Path) -> None:
    copter(tmp_path / "ok.bin", seed=1)
    good = tmp_path / "good.yaml"
    good.write_text(
        yaml.safe_dump({"logs": {"ok.bin": {"verdict": "healthy", "exhaustive": True}}})
    )
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    result = CliRunner().invoke(
        app, ["validate", str(good), "--markdown", str(md), "--json", str(js)]
    )
    assert result.exit_code == 0, result.output
    assert "1 of 1 logs fully correct" in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["passed"] == 1

    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"logs": {"ok.bin": {"must": ["gps.quality:problem"]}}}))
    result = CliRunner().invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1
    assert "missed gps.quality:problem" in result.output

    assert CliRunner().invoke(app, ["validate", str(tmp_path / "none")]).exit_code == 64
