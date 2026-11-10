"""Synthetic tests for attitude, GPS, battery, motor, estimator and event checks."""

from __future__ import annotations

import numpy as np
import pytest

from flightlens.checks._util import hold, in_air_mask, resample, takeoff_times
from flightlens.checks.attitude import AttitudeTracking
from flightlens.checks.battery import BatterySag
from flightlens.checks.estimator import EstimatorHealth
from flightlens.checks.events import EventsAndFailsafes
from flightlens.checks.gps import GpsQuality
from flightlens.checks.motors import MotorImbalance, MotorSaturation
from flightlens.engine import analyze
from flightlens.model import Event, EventKind, Flight, Severity

from .conftest import FlightFactory, make_signal

T = np.arange(0.0, 120.0, 0.05)  # 20 Hz
SEG = [(5.0, 115.0)]


def run(check, flight: Flight):  # type: ignore[no-untyped-def]
    return analyze(flight, checks=[check]).findings


# --- helpers ------------------------------------------------------------------


def test_resample_does_not_bridge_gaps() -> None:
    sig = make_signal("s", np.array([0.0, 1.0, 5.0, 6.0]), np.array([0.0, 1.0, 5.0, 6.0]))
    out = resample(sig, np.array([0.5, 3.0, 5.5, 7.0]), max_gap=1.5)
    assert out[0] == pytest.approx(0.5)
    assert np.isnan(out[1])  # inside a 4 s gap
    assert out[2] == pytest.approx(5.5)
    assert np.isnan(out[3])  # after the end


def test_hold_and_in_air(make_flight: FlightFactory) -> None:
    landed_t = np.array([0.0, 10.0, 100.0])
    flight = make_flight({"state.landed": (landed_t, np.array([1.0, 0.0, 1.0]))}, segments=SEG)
    t = np.array([2.0, 8.0, 50.0, 110.0])
    assert list(hold(flight.signal("state.landed"), t)) == [1, 1, 0, 1]
    assert list(in_air_mask(flight, t)) == [False, False, True, False]
    assert takeoff_times(flight) == [10.0]


# --- attitude -----------------------------------------------------------------


def attitude_flight(make_flight: FlightFactory, lag: np.ndarray) -> Flight:
    des = 10 * np.sin(T / 3)
    sigs = {}
    for axis in ("roll", "pitch", "yaw"):
        sigs[f"att.des_{axis}"] = (T, des)
        sigs[f"att.{axis}"] = (T, des + (lag if axis == "roll" else 0.5))
    return make_flight(sigs, segments=SEG)


def test_attitude_good_tracking(make_flight: FlightFactory) -> None:
    [f] = run(AttitudeTracking(), attitude_flight(make_flight, np.full_like(T, 1.0)))
    assert f.severity is Severity.OK


def test_attitude_sustained_error_is_problem(make_flight: FlightFactory) -> None:
    lag = np.where((T > 40) & (T < 44), 12.0, 1.0)
    [f] = run(AttitudeTracking(), attitude_flight(make_flight, lag))
    assert f.severity is Severity.PROBLEM
    assert f.title == "Poor roll tracking"
    assert f.start == pytest.approx(40, abs=0.1)


def test_attitude_brief_excursions_are_not_sustained(make_flight: FlightFactory) -> None:
    # 12° errors that drop back every 0.5 s never hold for 2 s.
    lag = np.where((T > 40) & (T < 50) & ((T % 1.0) < 0.5), 12.0, 1.0)
    [f] = run(AttitudeTracking(), attitude_flight(make_flight, lag))
    assert f.severity is Severity.OK


def test_attitude_ignored_when_controller_inactive(make_flight: FlightFactory) -> None:
    flight = attitude_flight(make_flight, np.full_like(T, 20.0))
    flight.signals["state.att_ctrl"] = make_signal(
        "state.att_ctrl", np.array([0.0]), np.array([0.0])
    )
    [f] = run(AttitudeTracking(), flight)
    assert f.severity is Severity.NOT_EVALUATED


# --- GPS ------------------------------------------------------------------------


def gps_flight(make_flight: FlightFactory, sats, hdop, fix) -> Flight:  # type: ignore[no-untyped-def]
    t = np.arange(0.0, 120.0, 0.2)
    full = lambda v: np.broadcast_to(np.asarray(v, dtype=float), t.shape).copy()  # noqa: E731
    return make_flight(
        {"gps.nsats": (t, full(sats)), "gps.hdop": (t, full(hdop)), "gps.fix": (t, full(fix))},
        segments=SEG,
    )


def test_gps_healthy(make_flight: FlightFactory) -> None:
    [f] = run(GpsQuality(), gps_flight(make_flight, 14, 0.8, 3))
    assert f.severity is Severity.OK


def test_gps_low_sats_attention_and_fix_loss_problem(make_flight: FlightFactory) -> None:
    t = np.arange(0.0, 120.0, 0.2)
    sats = np.where((t > 30) & (t < 40), 7.0, 14.0)
    fix = np.where((t > 80) & (t < 85), 2.0, 3.0)
    findings = run(GpsQuality(), gps_flight(make_flight, sats, 0.8, fix))
    titles = {f.title: f.severity for f in findings}
    assert titles["GPS 3D fix lost"] is Severity.PROBLEM


def test_gps_never_fixed_is_not_evaluated(make_flight: FlightFactory) -> None:
    [f] = run(GpsQuality(), gps_flight(make_flight, 0, 99, 0))
    assert f.severity is Severity.NOT_EVALUATED


# --- battery ------------------------------------------------------------------


def battery_flight(make_flight: FlightFactory, volts: np.ndarray, cells: float) -> Flight:
    return make_flight(
        {"bat.volt": (T, volts), "bat.cells": (T, np.full_like(T, cells))}, segments=SEG
    )


def test_battery_healthy(make_flight: FlightFactory) -> None:
    volts = np.linspace(16.6, 14.8, T.size)
    [f] = run(BatterySag(), battery_flight(make_flight, volts, 4))
    assert f.severity is Severity.OK
    lowest_armed = volts[T <= 115.0].min() / 4  # disarm at 115 s
    assert f.measured == pytest.approx(lowest_armed)


def test_battery_sag_problem(make_flight: FlightFactory) -> None:
    v = np.full_like(T, 15.2)
    v[(T > 100) & (T < 104)] = 13.0  # 3.25 V/cell
    [f] = run(BatterySag(), battery_flight(make_flight, v, 4))
    assert f.severity is Severity.PROBLEM
    assert f.comparison == "<"


def test_battery_implausible_cell_count(make_flight: FlightFactory) -> None:
    [f] = run(BatterySag(), battery_flight(make_flight, np.full_like(T, 25.0), 1))
    assert f.severity is Severity.NOT_EVALUATED
    assert "implausible" in f.explanation


# --- motors -------------------------------------------------------------------


def motor_flight(make_flight: FlightFactory, outs: list[np.ndarray]) -> Flight:
    sigs = {f"mot.{i + 1}": (T, o) for i, o in enumerate(outs)}
    for axis in ("roll", "pitch", "yaw"):
        sigs[f"rate.{axis}"] = (T, np.zeros_like(T))
    return make_flight(sigs, segments=SEG)


def test_motors_balanced(make_flight: FlightFactory) -> None:
    flight = motor_flight(make_flight, [np.full_like(T, 0.5)] * 4)
    assert run(MotorImbalance(), flight)[0].severity is Severity.OK
    assert run(MotorSaturation(), flight)[0].severity is Severity.OK


def test_motor_imbalance_names_the_motor(make_flight: FlightFactory) -> None:
    outs = [np.full_like(T, 0.5)] * 4
    outs[2] = np.full_like(T, 0.62)
    [f] = run(MotorImbalance(), motor_flight(make_flight, outs))
    assert f.severity is Severity.ATTENTION
    assert f.title == "Motor 3 works harder than the others"


def test_motor_saturation_problem(make_flight: FlightFactory) -> None:
    outs = [np.full_like(T, 0.5)] * 4
    outs[0] = np.where((T > 50) & (T < 58), 1.0, 0.5)
    [f] = run(MotorSaturation(), motor_flight(make_flight, outs))
    assert f.severity is Severity.PROBLEM
    assert f.title == "Motor 1 at full power"
    assert f.measured == pytest.approx(8, abs=0.2)


# --- estimator ------------------------------------------------------------------


def test_estimator_names_worst_sensor(make_flight: FlightFactory) -> None:
    hgt = np.full_like(T, 0.1)
    mag = np.where((T > 60) & (T < 63), 1.4, 0.2)
    [f] = run(EstimatorHealth(), make_flight({"ekf.hgt": (T, hgt), "ekf.mag": (T, mag)}, SEG))
    assert f.severity is Severity.PROBLEM
    assert f.evidence_signals == ("ekf.mag",)


def test_estimator_ignores_heading_realignment_after_takeoff(make_flight: FlightFactory) -> None:
    mag = np.where((T > 20) & (T < 24), 2.0, 0.2)  # spike right after takeoff at 19 s
    flight = make_flight(
        {
            "ekf.hgt": (T, np.full_like(T, 0.1)),
            "ekf.mag": (T, mag),
            "state.landed": (np.array([0.0, 19.0]), np.array([1.0, 0.0])),
        },
        SEG,
    )
    [f] = run(EstimatorHealth(), flight)
    assert f.severity is Severity.OK


# --- events ---------------------------------------------------------------------


def test_events_failsafe_in_flight_is_problem(make_flight: FlightFactory) -> None:
    flight = make_flight({}, SEG)
    flight.arming_known = True
    flight.events = [
        Event(1.0, EventKind.ERROR, "Preflight fail: baro", "error"),  # on the ground
        Event(60.0, EventKind.FAILSAFE, "Failsafe triggered", "critical"),
        Event(70.0, EventKind.ERROR, "ESC 2 timeout", "error"),
    ]
    findings = run(EventsAndFailsafes(), flight)
    assert [f.severity for f in findings] == [Severity.PROBLEM, Severity.ATTENTION]
    assert findings[1].title == "ESC 2 timeout"


def test_events_without_status_are_not_evaluated(make_flight: FlightFactory) -> None:
    [f] = run(EventsAndFailsafes(), make_flight({}, SEG))
    assert f.severity is Severity.NOT_EVALUATED
