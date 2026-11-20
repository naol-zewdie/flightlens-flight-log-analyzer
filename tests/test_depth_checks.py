"""Phase 2 depth checks: rate-loop tuning, power, battery reserve, RC link,
compass interference and estimator lane switches."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from flightlens import parsers
from flightlens.checks import all_checks
from flightlens.engine import analyze
from flightlens.model import Flight, Severity
from flightlens.sim.benchmark import Fault, copter
from flightlens.tuning import analyze_axis, coherence

from .conftest import FlightFactory


def run(check_id: str, flight: Flight, **overrides: float) -> list:  # type: ignore[type-arg]
    check = next(c for c in all_checks() if c.id == check_id)
    return check.run(flight, {**check.defaults, **overrides})


# --- rate-loop step response --------------------------------------------------------------


def _second_order(zeta: float, wn: float = 35.0, seconds: float = 120.0, seed: int = 0):  # type: ignore[no-untyped-def]
    """100 Hz setpoint steps and the response of a 2nd-order loop, with gyro noise."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, seconds, 0.01)
    sp = np.zeros_like(t)
    level, next_step = 0.0, 0.0
    for i, ti in enumerate(t):
        if ti >= next_step:
            level, next_step = rng.normal(0, 30), ti + rng.uniform(0.25, 0.8)
        sp[i] = level
    x = v = 0.0
    y = np.zeros_like(t)
    for i in range(t.size):
        for _ in range(5):
            v += (wn**2 * (sp[i] - x) - 2 * zeta * wn * v) * 0.002
            x += v * 0.002
        y[i] = x
    return t, sp, y + rng.normal(0, 1.0, t.size)


@pytest.mark.parametrize("zeta", [0.75, 0.45, 0.2, 0.08])
def test_step_response_recovers_the_true_peak(make_flight: FlightFactory, zeta: float) -> None:
    t, sp, y = _second_order(zeta)
    flight = make_flight({"rate.des_roll": (t, sp), "rate.roll": (t, y)}, segments=[(0, t[-1])])
    r = analyze_axis(flight, "roll", 0.5, 5.0)
    assert r is not None and r.windows >= 100 and r.coherence > 0.9
    true_peak = 1 + np.exp(-np.pi * zeta / np.sqrt(1 - zeta**2))
    assert r.peak == pytest.approx(true_peak, abs=0.15)
    assert r.final == pytest.approx(1.0, abs=0.08)


def test_coherence_is_low_when_output_ignores_input() -> None:
    rng = np.random.default_rng(1)
    xs = [np.fft.rfft(rng.normal(size=400), 800) for _ in range(20)]
    ys = [np.fft.rfft(rng.normal(size=400), 800) for _ in range(20)]
    f = np.fft.rfftfreq(800, 1 / 100)
    assert coherence(xs, ys, f, 3.0) < 0.3
    assert coherence(xs, [2 * x for x in xs], f, 3.0) == pytest.approx(1.0)


def test_tuning_needs_a_reliable_estimate(make_flight: FlightFactory) -> None:
    t, sp, y = _second_order(0.08, seconds=20)  # few windows: shown, not judged
    flight = make_flight({"rate.des_roll": (t, sp), "rate.roll": (t, y)}, segments=[(0, t[-1])])
    (f,) = run("control.tuning", flight)
    assert f.severity is Severity.NOT_EVALUATED and "not reliable" in f.explanation


def test_overshoot_is_measured_above_where_the_loop_settles(make_flight: FlightFactory) -> None:
    t, sp, y = _second_order(0.75)
    # settles 30% high without ringing (feed-forward too high): not "overshoot"
    flight = make_flight(
        {"rate.des_roll": (t, sp), "rate.roll": (t, 1.3 * y)}, segments=[(0, t[-1])]
    )
    titles = [f.title for f in run("control.tuning", flight)]
    assert titles == ["Rate loops well tuned"]


def test_rate_far_from_setpoint_is_reported(make_flight: FlightFactory) -> None:
    t, sp, y = _second_order(0.75)
    flight = make_flight(
        {"rate.des_roll": (t, sp), "rate.roll": (t, 2.2 * y)}, segments=[(0, t[-1])]
    )
    (f,) = run("control.tuning", flight)
    assert f.title == "Roll rate does not follow its setpoint" and f.measured > 2


# --- power supply, battery reserve, RC link ---------------------------------------------------


def test_power_supply_levels(make_flight: FlightFactory) -> None:
    t = np.arange(0, 100, 0.2)
    v = np.full_like(t, 5.05)
    assert (
        run("power.supply", make_flight({"power.5v": (t, v)}, segments=[(0, 99)]))[0].severity
        is Severity.OK
    )
    v[200:205] = 4.25
    (f,) = run("power.supply", make_flight({"power.5v": (t, v)}, segments=[(0, 99)]))
    assert f.severity is Severity.PROBLEM and f.start == pytest.approx(40.0)


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (np.linspace(0.97, 0.25, 600), Severity.OK),
        (np.linspace(0.97, 0.15, 600), Severity.ATTENTION),
        (np.linspace(0.97, 0.05, 600), Severity.PROBLEM),
        (np.full(600, 1.0), Severity.NOT_EVALUATED),  # capacity not configured
    ],
)
def test_battery_reserve_at_landing(
    make_flight: FlightFactory, remaining: np.ndarray, expected: Severity
) -> None:
    t = np.linspace(0, 600, remaining.size)
    flight = make_flight({"bat.remaining": (t, remaining)}, segments=[(0, 600)])
    assert run("battery.reserve", flight)[0].severity is expected


def test_battery_estimate_that_follows_voltage_sag_is_not_trusted(
    make_flight: FlightFactory,
) -> None:
    t = np.linspace(0, 60, 300)
    # 50% to 0% in 40 s of flight (the real PX4 log showed 49% in 0.8 min)
    rem = np.clip(0.5 - t / 80, 0, None)
    (f,) = run("battery.reserve", make_flight({"bat.remaining": (t, rem)}, segments=[(0, 60)]))
    assert f.severity is Severity.NOT_EVALUATED and "voltage sag" in f.explanation
    # falls at a plausible rate over 10 min, but rises by 30 points in the air
    t = np.linspace(0, 600, 300)
    rem = np.concatenate([np.linspace(0.9, 0.4, 150), np.linspace(0.7, 0.6, 150)])
    (f,) = run("battery.reserve", make_flight({"bat.remaining": (t, rem)}, segments=[(0, 600)]))
    assert f.severity is Severity.NOT_EVALUATED and "rose" in f.explanation


def test_rc_link(make_flight: FlightFactory) -> None:
    t = np.arange(0, 100, 0.1)
    lost = np.zeros_like(t)
    assert (
        run("rc.link", make_flight({"rc.lost": (t, lost)}, segments=[(0, 99)]))[0].severity
        is Severity.OK
    )
    lost[500:503] = 1  # 0.3 s
    (f,) = run("rc.link", make_flight({"rc.lost": (t, lost)}, segments=[(0, 99)]))
    assert f.severity is Severity.ATTENTION
    lost[600:620] = 1  # plus 2 s
    (f,) = run("rc.link", make_flight({"rc.lost": (t, lost)}, segments=[(0, 99)]))
    assert f.severity is Severity.PROBLEM and f.measured == pytest.approx(2.3, abs=0.2)


# --- compass and estimator lanes ----------------------------------------------------------------


@pytest.mark.parametrize(("per_amp", "expected"), [(0.0005, "ok"), (0.008, "attention"),
                                                   (0.02, "problem")])  # fmt: skip
def test_compass_interference(make_flight: FlightFactory, per_amp: float, expected: str) -> None:
    rng = np.random.default_rng(0)
    t = np.arange(0, 200, 0.1)
    cur = 15 + 10 * np.sin(t / 7) ** 2 + rng.normal(0, 0.5, t.size)  # 15-25 A
    earth = np.array([0.22, 0.05, 0.42])
    flight = make_flight(
        {
            "mag.x": (t, earth[0] + rng.normal(0, 0.003, t.size)),
            "mag.y": (t, earth[1] + rng.normal(0, 0.003, t.size)),
            "mag.z": (t, earth[2] + per_amp * cur),
            "bat.curr": (t, cur),
        },
        segments=[(0, 200)],
    )
    (f,) = run("compass.interference", flight)
    assert f.severity.value == expected


def test_compass_needs_current_variation(make_flight: FlightFactory) -> None:
    t = np.arange(0, 100, 0.1)
    flat = np.full_like(t, 0.4)
    flight = make_flight(
        {"mag.x": (t, flat), "mag.y": (t, flat), "mag.z": (t, flat), "bat.curr": (t, flat * 50)},
        segments=[(0, 100)],
    )
    (f,) = run("compass.interference", flight)
    assert f.severity is Severity.NOT_EVALUATED


def test_lane_switches_count_only_in_the_air(make_flight: FlightFactory) -> None:
    t = np.arange(0, 100, 0.5)
    lanes = np.where(t < 5, 1.0, 0.0)  # a switch at 5 s, before takeoff
    flight = make_flight({"ekf.primary": (t, lanes)}, segments=[(10, 90)])
    assert run("estimator.lanes", flight)[0].severity is Severity.OK
    lanes = lanes.copy()
    lanes[(t >= 30) & (t < 40)] = 1
    lanes[t >= 60] = 2
    (f,) = run("estimator.lanes", make_flight({"ekf.primary": (t, lanes)}, segments=[(10, 90)]))
    assert f.severity is Severity.PROBLEM and f.measured == 3


# --- end to end, from a generated ArduPilot log -----------------------------------------------


def test_simulated_flight_has_every_new_signal(tmp_path: Path) -> None:
    flight = parsers.load(copter(tmp_path / "c.bin", None, 3))
    assert {"mag.x", "ekf.primary", "power.5v", "rate.des_roll"} <= set(flight.signals)
    report = analyze(flight)
    status = {r.check_id: r.status for r in report.results}
    for check in ("control.tuning", "power.supply", "battery.reserve", "compass.interference",
                  "estimator.lanes"):  # fmt: skip
        assert status[check] is Severity.OK, check
    flight = parsers.load(copter(tmp_path / "s.bin", Fault("tuning_sluggish", level=5.0), 3))
    titles = [f.title for f in analyze(flight).findings]
    assert "Roll rate response is sluggish" in titles
