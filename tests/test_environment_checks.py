"""ESC telemetry, barometer and wind checks, and the signals they read."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from flightlens import parsers
from flightlens.checks import all_checks
from flightlens.model import Flight, Severity
from flightlens.sim.benchmark import Fault, copter

from .conftest import FlightFactory

T = np.arange(0, 120, 0.1)
SEG = [(10.0, 110.0)]


def run(check_id: str, flight: Flight, **overrides: float) -> list:  # type: ignore[type-arg]
    check = next(c for c in all_checks() if c.id == check_id)
    return check.run(flight, {**check.defaults, **overrides})


def quad(
    make_flight: FlightFactory,
    rpm: list[np.ndarray],
    out: list[np.ndarray] | None = None,
    **extra: np.ndarray,
) -> Flight:
    sigs = {f"esc.{i + 1}.rpm": (T, r) for i, r in enumerate(rpm)}
    sigs |= {f"mot.{i + 1}": (T, o) for i, o in enumerate(out or [])}
    sigs |= {name.replace("_", "."): (T, v) for name, v in extra.items()}
    return make_flight(sigs, segments=SEG)


# --- ESC ----------------------------------------------------------------------------------


def test_esc_telemetry_of_zeros_is_not_evaluated(make_flight: FlightFactory) -> None:
    zeros = np.zeros_like(T)
    [f] = run("esc.health", quad(make_flight, [zeros] * 4))
    assert f.severity is Severity.NOT_EVALUATED


def test_reversed_motors_and_idle_dips_are_fine(make_flight: FlightFactory) -> None:
    spin = np.full_like(T, 9000.0)
    dip = spin.copy()
    dip[500:520] = 15  # 2 s near standstill, but the output is near idle too
    low = np.full_like(T, 0.5)
    low_out = low.copy()
    low_out[500:520] = 0.08
    # a VTOL's pusher pair turns the other way: negative RPM
    [f] = run("esc.health", quad(make_flight, [dip, spin, -spin, -spin], [low_out, low, low, low]))
    assert f.severity is Severity.OK and "every motor turned as commanded" in f.explanation.lower()


def test_a_motor_that_stops_while_commanded_is_a_problem(make_flight: FlightFactory) -> None:
    spin = np.full_like(T, 9000.0)
    stopped = spin.copy()
    stopped[600:610] = 150  # 1 s, commanded at 50%
    out = [np.full_like(T, 0.5)] * 4
    [f] = run("esc.health", quad(make_flight, [spin, stopped, spin, spin], out))
    assert f.severity is Severity.PROBLEM and f.title == "Motor 2 stopped while commanded"
    assert f.start is not None and 59.5 <= f.start <= 60.5
    # shorter than stall_min_s: not judged
    stopped[600:610] = 9000
    stopped[600:603] = 150
    [f] = run("esc.health", quad(make_flight, [spin, stopped, spin, spin], out))
    assert f.severity is Severity.OK


def test_hot_esc_and_planes_without_motor_outputs(make_flight: FlightFactory) -> None:
    temp = np.full_like(T, 50.0)
    temp[700:760] = 115
    flight = quad(make_flight, [np.full_like(T, 6000.0)], esc_1_temp=temp)
    [f] = run("esc.health", flight)
    assert f.severity is Severity.PROBLEM and f.title == "ESC 1 overheating"
    assert f.measured == 115 and f.unit == "°C"
    temp[700:760] = 60
    [f] = run("esc.health", quad(make_flight, [np.full_like(T, 6000.0)], esc_1_temp=temp))
    assert f.severity is Severity.OK and "1 ESC reported" in f.explanation
    assert "only judged on multirotors" in f.explanation  # no motor outputs: RPM not judged


# --- wind ---------------------------------------------------------------------------------


def wind_flight(make_flight: FlightFactory, speed: np.ndarray) -> Flight:
    return make_flight({"wind.n": (T, speed), "wind.e": (T, np.zeros_like(T))}, segments=SEG)


def test_wind_that_is_never_estimated_is_not_evaluated(make_flight: FlightFactory) -> None:
    [f] = run("wind.speed", wind_flight(make_flight, np.zeros_like(T)))
    assert f.severity is Severity.NOT_EVALUATED and "EK3_DRAG_BCOEF" in f.explanation


def test_strong_wind_is_attention_never_problem(make_flight: FlightFactory) -> None:
    speed = np.full_like(T, 3.0)
    speed[400:900] = 25.0
    [f] = run("wind.speed", wind_flight(make_flight, speed))
    assert f.severity is Severity.ATTENTION and f.measured == 25.0
    # a burst at the very end of the data (an impact) is not wind
    speed = np.full_like(T, 3.0)
    speed[1095:1100] = 14.0
    [f] = run("wind.speed", wind_flight(make_flight, speed))
    assert f.severity is Severity.OK and "typically 3.0 m/s" in f.explanation


# --- barometer ----------------------------------------------------------------------------


def test_one_barometer_without_ground_periods_is_not_evaluated(make_flight: FlightFactory) -> None:
    [f] = run("baro.health", make_flight({"baro.alt": (T, np.zeros_like(T))}, segments=SEG))
    assert f.severity is Severity.NOT_EVALUATED


def test_two_barometers_that_disagree(make_flight: FlightFactory) -> None:
    alt = 10 + np.sin(T / 5)
    other = alt + 2.0  # a fixed offset is fine
    flight = make_flight({"baro.alt": (T, alt), "baro.alt2": (T, other)}, segments=SEG)
    [f] = run("baro.health", flight)
    assert f.severity is Severity.OK and "agreed" in f.explanation
    other[300:400] += 7
    flight = make_flight({"baro.alt": (T, alt), "baro.alt2": (T, other)}, segments=SEG)
    [f] = run("baro.health", flight)
    assert f.severity is Severity.ATTENTION and f.title == "Barometers disagree"


# --- end to end through the ArduPilot reader -------------------------------------------------


def test_signals_are_read_from_a_dataflash_log(tmp_path: Path) -> None:
    flight = parsers.load(copter(tmp_path / "f.bin", Fault("baro_drift", level=10), seed=3))
    for name in ("esc.1.rpm", "esc.4.temp", "esc.2.curr", "baro.alt", "baro.alt2", "wind.n"):
        assert name in flight.signals, name
    assert 30 < np.nanmax(flight.signal("esc.1.temp").values) < 50  # centi-degrees scaled
    drift = next(f for f in run_all(flight) if f.check_id == "baro.health")
    assert drift.severity is Severity.PROBLEM and 9 < (drift.measured or 0) < 11


def run_all(flight: Flight) -> list:  # type: ignore[type-arg]
    from flightlens.engine import analyze

    return analyze(flight).findings


def test_baro_rest_windows_never_reach_into_the_next_sortie(make_flight: FlightFactory) -> None:
    """Two launches 6 s apart: a long rest window after the first must stop before the
    second, or it measures the barometer in flight against GPS, which lags."""
    t = np.arange(0, 80, 0.1)
    flying = ((t >= 12) & (t < 38)) | ((t >= 44) & (t < 70))
    alt = np.where((t >= 12) & (t < 38), 10.0, 0.0)
    alt = np.where((t >= 44) & (t < 70), np.minimum((t - 44) * 10, 60.0), alt)
    gps = 500 + np.interp(t - 1.5, t, alt)  # GPS altitude lags behind a fast climb
    flight = make_flight(
        {
            "baro.alt": (t, alt),
            "gps.alt": (t, gps),
            "pos.rel_alt": (t, alt),
            "state.landed": (t, (~flying).astype(float)),
        },
        segments=[(10, 75)],
    )
    [f] = run("baro.health", flight, rest_window_s=10)
    assert f.severity is not Severity.PROBLEM and f.severity is not Severity.ATTENTION, f
