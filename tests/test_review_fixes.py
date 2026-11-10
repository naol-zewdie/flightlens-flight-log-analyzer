"""Regression tests for bugs found in the pre-commit review."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from typer.testing import CliRunner

from flightlens import cli, parsers
from flightlens.checks import all_checks
from flightlens.engine import Verdict, analyze
from flightlens.model import CheckConfig, Finding, Flight
from flightlens.summary import summarize

from .conftest import FlightFactory, vibe_flight

runner = CliRunner()


class _AlwaysOk:
    title = "Always OK"
    version = "1.0.0"
    required_signals: tuple[str, ...] = ()
    defaults: dict[str, Any] = {}

    def __init__(self, i: int) -> None:
        self.id = f"ok.{i}"

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        return []


def _no_arming(make_flight: FlightFactory, duration: float | None) -> Flight:
    flight = make_flight()
    md = dataclasses.replace(flight.metadata, log_duration=duration)
    return dataclasses.replace(flight, metadata=md)


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(2.0, Verdict.INSUFFICIENT_DATA), (None, Verdict.INSUFFICIENT_DATA), (60.0, Verdict.HEALTHY)],
)
def test_short_log_without_arming_info_is_not_healthy(
    make_flight: FlightFactory, duration: float | None, expected: Verdict
) -> None:
    checks = [_AlwaysOk(i) for i in range(3)]
    report = analyze(_no_arming(make_flight, duration), checks=checks)  # type: ignore[arg-type]
    assert report.verdict is expected


def _two_flights(make_flight: FlightFactory, name: str, values: np.ndarray) -> Flight:
    t = np.arange(0.0, 100.0, 1.0)
    return make_flight({name: (t, values)}, segments=[(0.0, 40.0), (60.0, 99.0)])


def test_clipping_while_disarmed_between_flights_is_not_counted(
    make_flight: FlightFactory,
) -> None:
    t = np.arange(0.0, 100.0, 1.0)
    clip = np.where(t >= 50, 500.0, 0.0)  # all 500 events happen on the ground
    flight = _two_flights(make_flight, "vibe.clip", clip)
    check = next(c for c in all_checks() if c.id == "imu.clipping")
    report = analyze(flight, checks=[check])
    assert report.findings[0].severity.value == "ok"


def test_summary_ignores_disarmed_gap(make_flight: FlightFactory) -> None:
    t = np.arange(0.0, 100.0, 1.0)
    used = np.where(t < 50, t, t + 1000.0)  # 1000 mAh "used" while disarmed
    flight = _two_flights(make_flight, "bat.used_mah", used)
    assert summarize(flight).battery_used_mah == pytest.approx(40.0 + 39.0)


def test_unwritable_json_path_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_flight: FlightFactory
) -> None:
    t = np.arange(0.0, 300.0, 0.1)
    flight = vibe_flight(make_flight, np.full_like(t, 68.0), t)
    monkeypatch.setattr(parsers, "load", lambda _path: flight)
    log = tmp_path / "flight.bin"
    log.write_bytes(b"\xa3\x95")
    out = tmp_path / "missing-dir" / "x.json"
    result = runner.invoke(cli.app, ["analyze", str(log), "--json", str(out)])
    assert result.exit_code == cli.EXIT_CANNOT_ANALYZE


def test_internal_error_exits_3_not_attention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_path: Path) -> Flight:
        raise RuntimeError("bug")

    monkeypatch.setattr(parsers, "load", boom)
    log = tmp_path / "flight.bin"
    log.write_bytes(b"\xa3\x95")
    monkeypatch.setattr(sys, "argv", ["flightlens", "analyze", str(log)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == cli.EXIT_CANNOT_ANALYZE
