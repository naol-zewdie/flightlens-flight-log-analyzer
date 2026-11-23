"""Rules learned from real Mission Planner logs: what counts as a flight.

On a ground run-up at 95% throttle, prop-wash made the barometric altitude jump
by ±0.5 m between 2 Hz telemetry samples; one ArduPlane launch crashed after
climbing 1.1 m in its last second; a taxi was armed for 21 s without climbing.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import climbed
from flightlens.engine import Verdict, analyze
from flightlens.model import Event, EventKind, Flight
from flightlens.parsers.tlog import _message_event

from .conftest import FlightFactory


def test_noisy_altitude_is_not_a_climb(make_flight: FlightFactory) -> None:
    rng = np.random.default_rng(0)
    t = np.arange(0, 30, 0.5)  # 2 Hz telemetry
    alt = rng.normal(0, 0.3, t.size)
    alt[[10, 40]] = [-0.75, 0.47]  # the extremes seen on the real run-up
    flight = make_flight({"pos.rel_alt": (t, alt)}, segments=[(0, 30)])
    assert float(alt.max() - alt.min()) > 1.0
    assert climbed(flight) == [False]


def test_a_short_real_climb_at_the_end_counts(make_flight: FlightFactory) -> None:
    t = np.arange(0, 57, 0.04)  # 25 Hz onboard log
    alt = np.where(t < 55.5, -0.2 + 0.02 * np.sin(t), -0.2 + (t - 55.5) * 0.85)
    flight = make_flight({"pos.rel_alt": (t, alt)}, segments=[(0, 57)])
    assert climbed(flight) == [True]


def test_a_steady_climb_counts_at_any_rate(make_flight: FlightFactory) -> None:
    for dt in (0.5, 0.1, 0.02):
        t = np.arange(0, 60, dt)
        alt = np.clip((t - 10) * 1.5, 0, 15)
        assert climbed(make_flight({"pos.rel_alt": (t, alt)}, segments=[(0, 60)])) == [True]


def _ground_only(make_flight: FlightFactory) -> Flight:
    """Armed 40 s, everything fine, never left the ground."""
    t = np.arange(0, 60, 0.1)
    flight = make_flight(
        {
            "pos.rel_alt": (t, np.zeros_like(t)),
            "vibe.x": (t, np.full_like(t, 10.0)),
            "vibe.y": (t, np.full_like(t, 10.0)),
            "vibe.z": (t, np.full_like(t, 10.0)),
        },
        segments=[(10, 50)],
    )
    flight.arming_known = True
    return flight


def test_healthy_needs_time_in_the_air(make_flight: FlightFactory) -> None:
    flight = _ground_only(make_flight)
    assert analyze(flight).verdict is Verdict.INSUFFICIENT_DATA
    # the same flight, but it climbs: now it can be called healthy if the checks pass
    t = flight.signals["pos.rel_alt"].t
    alt = np.clip((t - 12) * 2, 0, 10) * (t < 48)
    flight2 = make_flight(
        {n: (s.t, s.values) for n, s in flight.signals.items() if n != "pos.rel_alt"}
        | {"pos.rel_alt": (t, alt)},
        segments=[(10, 50)],
    )
    flight2.arming_known = True
    assert analyze(flight2).verdict is not Verdict.INSUFFICIENT_DATA


def test_betaflight_arming_blockers_explain_why_it_never_armed(
    make_flight: FlightFactory,
) -> None:
    flight = make_flight()
    flight.arming_known = True
    flight.events = [
        _message_event(5.0, 4, "Arming disabled: THROTTLE ANGLE"),
        _message_event(9.0, 4, "Arming disabled: RXLOSS MSP"),
    ]
    report = analyze(flight)
    (f,) = [f for f in report.findings if f.check_id == "preflight.arming"]
    assert f.severity.value == "attention" and "THROTTLE ANGLE" in f.explanation


def test_ardupilot_prearm_texts_are_not_in_flight_errors() -> None:
    # ArduPilot sends PreArm failures as CRITICAL status texts
    e = _message_event(3.0, 2, "PreArm: Compass not calibrated")
    assert e.kind is EventKind.MESSAGE and e.level == "warning"
    assert _message_event(3.0, 2, "Radio Failsafe").kind is EventKind.FAILSAFE
    assert isinstance(_message_event(3.0, 6, "Mission complete"), Event)
