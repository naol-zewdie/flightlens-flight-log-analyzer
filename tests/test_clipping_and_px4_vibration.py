from __future__ import annotations

import numpy as np

from flightlens.checks.clipping import ImuClipping
from flightlens.checks.vibration import VibrationLevel
from flightlens.engine import Verdict, analyze
from flightlens.model import Severity

from .conftest import FlightFactory

T = np.arange(0.0, 120.0, 1.0)  # PX4 vehicle_imu_status is logged at about 1 Hz


def metric_flight(make_flight: FlightFactory, values: np.ndarray) -> object:
    return make_flight({"vibe.metric": (T, values)}, segments=[(5.0, 115.0)])


def test_px4_metric_sustained_above_attention(make_flight: FlightFactory) -> None:
    v = np.full_like(T, 8.0)
    v[(T >= 40) & (T <= 55)] = 25.0
    [f] = analyze(metric_flight(make_flight, v), checks=[VibrationLevel()]).findings
    assert f.severity is Severity.ATTENTION
    assert f.title == "Vibration too high (worst IMU)"
    assert (f.start, f.end) == (40.0, 55.0)
    assert f.threshold == 20.0


def test_px4_metric_short_spike_is_ignored(make_flight: FlightFactory) -> None:
    v = np.full_like(T, 8.0)
    v[60:62] = 45.0  # 1 s above the limit, shorter than the 3 s minimum
    [f] = analyze(metric_flight(make_flight, v), checks=[VibrationLevel()]).findings
    assert f.severity is Severity.OK


def test_px4_metric_all_zero_is_not_evaluated(make_flight: FlightFactory) -> None:
    flight = metric_flight(make_flight, np.zeros_like(T))
    [f] = analyze(flight, checks=[VibrationLevel()]).findings
    assert f.severity is Severity.NOT_EVALUATED


def clip_flight(make_flight: FlightFactory, counts: np.ndarray) -> object:
    return make_flight({"vibe.clip": (T, counts)}, segments=[(5.0, 115.0)])


def test_no_clipping_is_ok(make_flight: FlightFactory) -> None:
    [f] = analyze(clip_flight(make_flight, np.full_like(T, 12.0)), checks=[ImuClipping()]).findings
    assert f.severity is Severity.OK


def test_clipping_counts_only_armed_increases(make_flight: FlightFactory) -> None:
    c = np.zeros_like(T)
    c[T >= 2] = 500  # before arming: ignored
    c[T >= 50] = 530  # in flight: 30 new events
    [f] = analyze(clip_flight(make_flight, c), checks=[ImuClipping()]).findings
    assert f.severity is Severity.ATTENTION
    assert f.measured == 30
    assert f.start == 49.0


def test_heavy_clipping_is_problem_and_counter_reset_is_safe(make_flight: FlightFactory) -> None:
    c = np.cumsum(np.where(T > 10, 5.0, 0.0))
    c[T >= 80] -= c[T >= 80][0]  # counter reset mid-flight
    [f] = analyze(clip_flight(make_flight, c), checks=[ImuClipping()]).findings
    assert f.severity is Severity.PROBLEM
    assert f.measured > 100


def test_all_checks_not_evaluated_gives_insufficient_data(make_flight: FlightFactory) -> None:
    report = analyze(make_flight({}), checks=[VibrationLevel(), ImuClipping()])
    assert report.verdict is Verdict.INSUFFICIENT_DATA
    assert report.verdict.exit_code == 3


def test_never_armed_flight_evaluates_nothing(make_flight: FlightFactory) -> None:
    flight = make_flight({"vibe.clip": (T, np.arange(T.size, dtype=float))})
    flight.arming_known = True  # arming recorded, no segments: never armed
    [f] = analyze(flight, checks=[ImuClipping()]).findings
    assert f.severity is Severity.NOT_EVALUATED
