from __future__ import annotations

import numpy as np
import pytest

from flightlens.checks.vibration import VibrationLevel
from flightlens.engine import analyze
from flightlens.model import Severity

from .conftest import FlightFactory, vibe_flight

T = np.arange(0.0, 300.0, 0.1)  # 5 minutes at 10 Hz
CHECK = VibrationLevel()


def run(flight):  # type: ignore[no-untyped-def]
    return analyze(flight, checks=[CHECK]).findings


def test_healthy_flight_gives_single_ok(make_flight: FlightFactory) -> None:
    z = 15 + 3 * np.sin(T)
    findings = run(vibe_flight(make_flight, z, T))
    assert [f.severity for f in findings] == [Severity.OK]
    assert findings[0].measured == pytest.approx(18, abs=0.1)


def test_attention_window_matches_injected_fault(make_flight: FlightFactory) -> None:
    z = np.full_like(T, 15.0)
    z[(T >= 120) & (T <= 180)] = 45.0
    [f] = run(vibe_flight(make_flight, z, T))
    assert f.severity is Severity.ATTENTION
    assert f.title == "Vibration too high (Z axis)"
    assert f.threshold == 30.0
    assert f.start == pytest.approx(120, abs=0.11)
    assert f.end == pytest.approx(180, abs=0.11)


def test_problem_above_upper_threshold(make_flight: FlightFactory) -> None:
    z = np.full_like(T, 15.0)
    z[(T >= 130) & (T <= 245)] = 68.0
    [f] = run(vibe_flight(make_flight, z, T))
    assert f.severity is Severity.PROBLEM
    assert f.measured == 68.0
    assert f.threshold == 60.0


def test_short_dips_are_merged_into_one_window(make_flight: FlightFactory) -> None:
    z = np.full_like(T, 15.0)
    z[(T >= 100) & (T <= 110)] = 40.0
    z[(T >= 110.5) & (T <= 120)] = 40.0  # 0.4 s dip, should merge
    [f] = run(vibe_flight(make_flight, z, T))
    assert (f.start, f.end) == pytest.approx((100, 120), abs=0.11)


def test_spike_while_disarmed_is_ignored(make_flight: FlightFactory) -> None:
    z = np.full_like(T, 15.0)
    z[T < 10] = 90.0  # ground handling before arming
    findings = run(vibe_flight(make_flight, z, T, segments=[(20.0, 290.0)]))
    assert [f.severity for f in findings] == [Severity.OK]


def test_thresholds_can_be_overridden(make_flight: FlightFactory) -> None:
    z = np.full_like(T, 25.0)
    flight = vibe_flight(make_flight, z, T)
    report = analyze(flight, checks=[CHECK], config={"vibration.level": {"attention": 20.0}})
    assert report.findings[0].severity is Severity.ATTENTION


def test_all_nan_is_not_evaluated(make_flight: FlightFactory) -> None:
    nan = np.full_like(T, np.nan)
    flight = make_flight({"vibe.x": (T, nan), "vibe.y": (T, nan), "vibe.z": (T, nan)})
    [f] = run(flight)
    assert f.severity is Severity.NOT_EVALUATED


def test_missing_axis_is_not_evaluated(make_flight: FlightFactory) -> None:
    flight = make_flight({"vibe.x": (T, T * 0), "vibe.y": (T, T * 0)})
    [f] = run(flight)
    assert f.severity is Severity.NOT_EVALUATED
    assert "vibe.z" in f.explanation
