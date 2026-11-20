"""Fixed-wing behaviour on generated ArduPlane logs, and "it never flew" diagnoses.

Modelled on real ArduPlane crash logs: runway takeoff at 20 s, Stabilize mode
(no yaw or roll setpoint: DesRoll/DesYaw stay 0), and one of three endings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from flightlens import parsers
from flightlens.engine import Verdict, analyze
from flightlens.model import Event, EventKind, Finding, Flight, FlightMetadata, Segment, Severity
from flightlens.sim.plane import plane_log

from .conftest import FlightFactory, make_signal


def _report(tmp_path: Path, **kw: Any) -> Any:
    return analyze(parsers.load(plane_log(tmp_path / "plane.bin", **kw)))


def _by_check(report: Any, check_id: str) -> list[Finding]:
    return [f for f in report.findings if f.check_id == check_id]


def test_smooth_plane_flight_has_no_false_alarms(tmp_path: Path) -> None:
    report = _report(tmp_path)
    flight = report.flight
    assert flight.metadata.vehicle_type == "Fixed wing"
    assert flight.signals["state.landed"].source.startswith("STAT.isFlying")
    issues = [(f.check_id, f.title) for f in report.findings if f.severity.rank >= 2]
    assert issues == [], issues
    # No yaw setpoint on a plane, and a constant DesRoll is not a setpoint either.
    assert _by_check(report, "attitude.tracking")[0].severity is Severity.NOT_EVALUATED
    summary = report.to_dict()["summary"]
    assert len(summary["sorties"]) == 1
    assert summary["airborne_s"] == pytest.approx(80, abs=1)


def test_crash_is_diagnosed_with_its_causes(tmp_path: Path) -> None:
    report = _report(tmp_path, ending="crash", trim=-0.2)
    first = report.findings[0]
    assert (first.check_id, first.title, first.severity) == (
        "flight.crash",
        "Crash",
        Severity.PROBLEM,
    )
    assert "upset" in first.explanation and "log ends" in first.explanation
    assert "Bank angle beyond the 45° limit" in first.related
    assert "Aileron at full travel" in first.related
    assert "Aircraft out of trim (roll)" in first.related
    # The impact itself must not be reported as vibration, clipping or a battery fault.
    for check_id in ("vibration.level", "imu.clipping"):
        assert all(f.severity.rank < 2 for f in _by_check(report, check_id)), check_id
    trim = _by_check(report, "control.trim")[0]
    assert "20% left aileron" in trim.explanation


def test_hard_landing_is_attention_not_crash(tmp_path: Path) -> None:
    report = _report(tmp_path, ending="hard")
    crash = _by_check(report, "flight.crash")
    assert [(f.title, f.severity) for f in crash] == [("Hard landing", Severity.ATTENTION)]
    assert all(f.severity.rank < 2 for f in _by_check(report, "vibration.level"))


# --- "it never flew" -------------------------------------------------------------


def _flight(make_flight: FlightFactory, **kw: Any) -> Flight:
    f = make_flight(**kw)
    f.arming_known = True
    f.metadata = FlightMetadata(log_format="ulog", vehicle_type="Quadrotor", log_duration=120.0)
    return f


def test_refused_arming_is_explained_but_not_judged(make_flight: FlightFactory) -> None:
    flight = _flight(make_flight)
    flight.events = [
        Event(
            10,
            EventKind.MESSAGE,
            "[health_and_arming_checks] Preflight Fail: height estimate not stable",
            "warning",
        ),
        Event(
            11,
            EventKind.ERROR,
            "[commander] Arming denied: Resolve system health failures first",
            "error",
        ),
        Event(
            30,
            EventKind.ERROR,
            "[commander] Arming denied: Resolve system health failures first",
            "error",
        ),
    ]
    report = analyze(flight)
    assert report.verdict is Verdict.INSUFFICIENT_DATA and report.score is None
    arming = _by_check(report, "preflight.arming")[0]
    assert arming.severity is Severity.ATTENTION
    assert "height estimate not stable" in arming.explanation and "2×" in arming.explanation


def test_takeoff_that_never_left_the_ground(make_flight: FlightFactory) -> None:
    t = np.arange(0.0, 60.0, 0.1)
    flight = _flight(
        make_flight,
        signals={
            "pos.rel_alt": (t, np.zeros_like(t)),
            "mot.1": (t, np.full_like(t, 0.3)),
            "mot.2": (t, np.full_like(t, 0.3)),
            "mot.3": (t, np.full_like(t, 0.3)),
            "mot.4": (t, np.where(t > 10, 1.0, 0.3)),  # one motor at full: symptom, not cause
            "att.roll": (t, np.zeros_like(t)),
            "att.pitch": (t, np.zeros_like(t)),
            "att.des_roll": (t, np.where(t > 10, 15.0, 0.0)),
            "att.des_pitch": (t, np.zeros_like(t)),
        },
        segments=[(5.0, 59.0)],
    )
    flight.events = [Event(8, EventKind.MODE, "Takeoff")]
    flight.signals["state.landed"] = make_signal(
        "state.landed", np.array([0.0, 9.0]), np.array([1.0, 0.0])
    )
    report = analyze(flight)
    takeoff = _by_check(report, "flight.takeoff")
    assert [f.title for f in takeoff] == ["Takeoff failed"]
    assert report.findings[0].check_id == "flight.takeoff"
    # On the ground the motor and attitude "problems" are symptoms; they must stay quiet.
    for check_id in ("motor.saturation", "motor.imbalance", "attitude.tracking"):
        assert all(f.severity.rank < 2 for f in _by_check(report, check_id)), check_id
    assert report.to_dict()["summary"]["sorties"] == []
    assert flight.segments == [Segment(5.0, 59.0)]


def test_mode_selected_before_arming_is_listed(tmp_path: Path) -> None:
    summary = _report(tmp_path).to_dict()["summary"]
    assert summary["modes"] == ["Stabilize"]  # chosen at 0 s, armed at 5 s
