from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from flightlens import cli, parsers

from .conftest import FlightFactory, vibe_flight

runner = CliRunner()


def test_checks_lists_vibration() -> None:
    result = runner.invoke(cli.app, ["checks"])
    assert result.exit_code == 0
    assert "vibration.level" in result.output


def test_unsupported_file_exits_3(tmp_path: Path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("not a log")
    result = runner.invoke(cli.app, ["analyze", str(p)])
    assert result.exit_code == cli.EXIT_CANNOT_ANALYZE


def test_problem_flight_exits_2_and_writes_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_flight: FlightFactory
) -> None:
    t = np.arange(0.0, 300.0, 0.1)
    z = np.where((t >= 130) & (t <= 245), 68.0, 15.0)
    flight = vibe_flight(make_flight, z, t)
    monkeypatch.setattr(parsers, "load", lambda _path: flight)

    log = tmp_path / "flight.bin"
    log.write_bytes(b"\xa3\x95")
    out = tmp_path / "report.json"
    result = runner.invoke(cli.app, ["analyze", str(log), "--json", str(out)])

    assert result.exit_code == 2, result.output
    assert "PROBLEM" in result.output
    assert "Vibration too high (Z axis)" in result.output
    assert "02:10–04:05" in result.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict"] == "problem"
    assert data["score"] == 70


def test_usage_error_exit_code_does_not_clash_with_verdicts() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "flightlens", "analyze"], capture_output=True, text=True
    )
    assert proc.returncode == cli.EXIT_USAGE
