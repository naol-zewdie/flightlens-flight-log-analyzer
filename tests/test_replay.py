"""Replay artifact: frame, attitude, resampling, and known-outcome flights."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flightlens import parsers
from flightlens.model import Flight, Signal
from flightlens.parsers.ulog import _quat_to_euler
from flightlens.replay import (
    MAX_GAP_S,
    REPLAY_VERSION,
    build_replay,
    euler_to_quat,
    local_frame,
    quat_to_euler,
    resample_quat,
    slerp,
    unwrap_yaw,
)
from flightlens.sim import manoeuvre

from .conftest import FlightFactory

LOGS = Path(__file__).parent.parent / "testdata" / "logs"


# --- frame --------------------------------------------------------------------------------


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    k = math.pi / 180
    a = (
        math.sin((lat2 - lat1) * k / 2) ** 2
        + math.cos(lat1 * k) * math.cos(lat2 * k) * math.sin((lon2 - lon1) * k / 2) ** 2
    )
    return 2 * 6_371_000.0 * math.asin(math.sqrt(a))


@pytest.mark.parametrize("lat0", [-35.3632, 0.0, 47.4, 64.1])
def test_local_frame_matches_great_circle_distances(lat0: float) -> None:
    lon0 = 8.55
    # 100 m north and 100 m east, by the sphere's own geometry
    north_lat = lat0 + math.degrees(100 / 6_371_000.0)
    east_lon = lon0 + math.degrees(100 / (6_371_000.0 * math.cos(math.radians(lat0))))
    e, n = local_frame(np.array([north_lat, lat0]), np.array([lon0, east_lon]), lat0, lon0)
    assert abs(n[0] - 100) < 0.1 and abs(e[0]) < 0.1
    assert abs(e[1] - 100) < 0.1 and abs(n[1]) < 0.1
    # a 700 m diagonal agrees with the great-circle distance to 0.1%
    lat, lon = lat0 + 0.004, lon0 + 0.004
    e, n = local_frame(np.array([lat]), np.array([lon]), lat0, lon0)
    d = _haversine(lat0, lon0, lat, lon)
    assert abs(math.hypot(e[0], n[0]) - d) / d < 1e-3


# --- attitude -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rpy",
    [(0, 0, 0), (30, 0, 0), (-30, 0, 0), (0, 45, 0), (0, 0, 90), (10, -20, 170), (0, 0, -179)],
)
def test_euler_quaternion_round_trip(rpy: tuple[float, float, float]) -> None:
    q = euler_to_quat(*(np.array([a], dtype=float) for a in rpy))
    assert abs(np.linalg.norm(q) - 1) < 1e-12
    back = quat_to_euler(q)
    for got, want in zip(back, rpy, strict=True):
        assert abs(((float(got[0]) - want) + 180) % 360 - 180) < 1e-9
    # the same convention as the PX4 parser's quaternion → Euler
    ulog = _quat_to_euler(*(q[:, i] for i in range(4)))
    for a, b in zip(back, ulog, strict=True):
        assert np.allclose(a, b)


def test_slerp_halfway_and_shortest_arc() -> None:
    q0 = euler_to_quat(np.array([0.0]), np.array([0.0]), np.array([0.0]))
    q1 = euler_to_quat(np.array([60.0]), np.array([0.0]), np.array([0.0]))
    roll, _, _ = quat_to_euler(slerp(q0, q1, np.array([0.5])))
    assert abs(roll[0] - 30) < 1e-9
    # -q is the same attitude: slerp must not take the long way round
    roll, _, _ = quat_to_euler(slerp(q0, -q1, np.array([0.5])))
    assert abs(roll[0] - 30) < 1e-9
    # across the heading wrap: 350° → 10° passes 0°, not 180°
    a = euler_to_quat(np.array([0.0]), np.array([0.0]), np.array([350.0]))
    b = euler_to_quat(np.array([0.0]), np.array([0.0]), np.array([10.0]))
    _, _, yaw = quat_to_euler(slerp(a, b, np.array([0.5])))
    assert abs(yaw[0]) < 1e-9


def test_unwrap_yaw_keeps_gaps() -> None:
    y = unwrap_yaw(np.array([350.0, 359.0, np.nan, 1.0, 10.0]))
    assert y[1] == 359.0 and np.isnan(y[2]) and y[3] == 361.0 and y[4] == 370.0


# --- resampling (property-based) ---------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.floats(0.01, 3.0), min_size=3, max_size=60),
    st.lists(st.floats(-180, 180), min_size=60, max_size=60),
)
def test_resampled_attitude_never_bridges_long_gaps(steps: list[float], yaws: list[float]) -> None:
    t = np.cumsum(steps)
    y = np.array(yaws[: t.size])
    q = euler_to_quat(np.zeros_like(t), np.zeros_like(t), y)
    grid = np.arange(t[0], t[-1], 0.04)
    out = resample_quat(t, q, grid)
    right = np.clip(np.searchsorted(t, grid, side="right"), 0, t.size - 1)
    left = np.clip(right - 1, 0, t.size - 1)
    in_gap = (t[right] - t[left]) > MAX_GAP_S
    assert np.all(np.isnan(out[in_gap]))
    assert np.all(np.isfinite(out[~in_gap]))
    norms = np.linalg.norm(out[~in_gap], axis=1)
    assert np.allclose(norms, 1.0)


def test_heading_wrap_never_passes_180(make_flight: FlightFactory) -> None:
    t = np.arange(0, 10, 0.1)
    yaw = np.where(t < 5, 355.0, 5.0)  # crosses north
    yaw[(t >= 4.9) & (t < 5.1)] = [358.0, 2.0]
    flight = make_flight(
        {
            "att.roll": (t, np.zeros_like(t)),
            "att.pitch": (t, np.zeros_like(t)),
            "att.yaw": (t, yaw),
        },
        segments=[(0, 10)],
    )
    r = build_replay(flight)
    y = r.columns["yaw"]
    assert np.all(np.abs(y[np.isfinite(y)]) < 10), "the model spun through south"
    tt = r.columns["t"]
    assert np.all(np.diff(tt) > 0) and np.allclose(np.diff(tt), 1 / 25)


# --- known-outcome flight -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def manoeuvre_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return manoeuvre.manoeuvre_log(tmp_path_factory.mktemp("m") / "manoeuvres.bin")


def _check_against_truth(flight: Flight, deg: float, metres: float) -> None:
    r = build_replay(flight)
    c = r.columns
    assert r.meta["origin"]["chosen"] == "take-off point"
    worst_att = worst_pos = 0.0
    for i in range(c["t"].size):
        g = manoeuvre.truth(float(c["t"][i]))
        if c["valid_att"][i]:
            for got, want in (
                (c["roll"][i], g.roll),
                (c["pitch"][i], g.pitch),
                (c["yaw"][i], g.yaw),
            ):
                worst_att = max(worst_att, abs((float(got) - want + 180) % 360 - 180))
        if c["valid_pos"][i] and np.isfinite(c["up"][i]):
            err = math.dist((c["east"][i], c["north"][i], c["up"][i]), (g.east, g.north, g.up))
            worst_pos = max(worst_pos, err)
    assert worst_att < deg, f"attitude off by {worst_att:.3f}°"
    assert worst_pos < metres, f"position off by {worst_pos:.3f} m"
    # the script's features are really there
    at = lambda s: int(np.argmin(np.abs(c["t"] - s)))  # noqa: E731
    assert abs(c["roll"][at(22)] - 30) < deg and abs(c["pitch"][at(32)] - 45) < deg
    assert abs(c["east"][at(60)] - 20) < metres and abs(c["north"][at(70)] - 20) < metres


def test_known_manoeuvres_onboard_log(manoeuvre_bin: Path) -> None:
    _check_against_truth(parsers.load(manoeuvre_bin), deg=0.5, metres=0.5)


def test_known_manoeuvres_telemetry_log(manoeuvre_bin: Path, tmp_path: Path) -> None:
    from flightlens.sim.telemetry import telemetry

    tlog = telemetry(parsers.load(manoeuvre_bin), tmp_path / "manoeuvres.tlog")
    flight = parsers.load(tlog)
    r = build_replay(flight)
    assert r.meta["rate_hz"] <= 10
    # telemetry is slower: the check is looser, but still within the plan's tolerance
    _check_against_truth(flight, deg=0.5, metres=0.5)


def test_generated_plane_points_where_it_flies(tmp_path: Path) -> None:
    """The demo planes' logged heading follows their track, and they bank into the turn
    (they once logged a heading that just rotated, so the 3D model flew sideways)."""
    from flightlens.sim.plane import plane_log

    c = build_replay(parsers.load(plane_log(tmp_path / "plane.bin"))).columns
    k = 25  # 1 s baseline at 25 Hz
    de, dn = c["east"][k:] - c["east"][:-k], c["north"][k:] - c["north"][:-k]
    speed = np.hypot(de, dn) / (c["t"][k:] - c["t"][:-k])
    track = np.degrees(np.arctan2(de, dn))
    mid = slice(k // 2, k // 2 + len(track))
    moving = speed > 10
    assert moving.sum() > 1000
    off = (c["yaw"][mid][moving] - track[moving] + 180) % 360 - 180
    assert np.percentile(np.abs(off), 95) < 5
    # a left-hand circuit: banked left in the turns, wings level on the straight legs
    turn_rate = np.gradient(np.unwrap(np.radians(c["yaw"]))) * 25
    turning = moving & (np.degrees(turn_rate[mid]) < -4)
    straight = moving & (np.abs(np.degrees(turn_rate[mid])) < 0.5) & (c["t"][mid] > 25)
    assert turning.sum() > 100 and straight.sum() > 100
    assert np.median(c["roll"][mid][turning]) < -8
    assert np.median(np.abs(c["roll"][mid][straight])) < 2


# --- formats and missing data ---------------------------------------------------------------


def test_every_format_replays(manoeuvre_bin: Path) -> None:
    px4 = build_replay(parsers.load(LOGS / "px4-v1.13-sitl-tagged.ulg"))
    assert px4.meta["sources"]["attitude"] == "logged quaternion"
    assert px4.meta["replay_version"] == REPLAY_VERSION
    ap = build_replay(parsers.load(manoeuvre_bin))
    assert ap.meta["sources"]["attitude"] == "roll, pitch and yaw"
    assert ap.meta["sources"]["position"] == "estimated position"
    assert ap.meta["modes"] and ap.meta["columns"][0] == "t"


def test_no_position_gives_an_attitude_only_replay(make_flight: FlightFactory) -> None:
    t = np.arange(0, 20, 0.1)
    flight = make_flight(
        {"att.roll": (t, np.full_like(t, 5.0)), "att.pitch": (t, np.zeros_like(t))},
        segments=[(1, 19)],
    )
    r = build_replay(flight)
    assert r.meta["origin"] is None and r.meta["sources"]["position"] is None
    assert r.meta["sources"]["attitude"] == "roll and pitch (no heading logged)"
    assert not r.columns["valid_pos"].any() and r.columns["valid_att"].all()


def test_a_long_dropout_is_a_gap_not_smoothed(make_flight: FlightFactory) -> None:
    t = np.concatenate([np.arange(0, 10, 0.1), np.arange(15, 25, 0.1)])  # 5 s missing
    flight = make_flight(
        {
            "att.roll": (t, np.zeros_like(t)),
            "att.pitch": (t, np.zeros_like(t)),
            "att.yaw": (t, np.zeros_like(t)),
        },
        segments=[(0, 25)],
    )
    r = build_replay(flight)
    tt, valid = r.columns["t"], r.columns["valid_att"]
    assert not valid[(tt > 10.1) & (tt < 14.9)].any()
    assert valid[(tt < 9.8) | (tt > 15.2)].all()
    [[a, b]] = r.meta["gaps"]
    assert 9.9 <= a <= 10.1 and 14.9 <= b <= 15.1


def test_written_and_read_back(manoeuvre_bin: Path, tmp_path: Path) -> None:
    from flightlens.replay import read_columns

    r = build_replay(parsers.load(manoeuvre_bin))
    r.write(tmp_path)
    back = read_columns(tmp_path)
    assert back is not None and set(back) == set(r.columns)
    assert np.allclose(back["east"], r.columns["east"], equal_nan=True)


def test_signal_helpers_reject_nothing_useful() -> None:
    # a signal of NaNs counts as absent, never as zeros
    s = Signal("att.roll", "deg", np.arange(3.0), np.full(3, np.nan))
    flight = Flight(metadata=parsers.load(LOGS / "px4-v1.13-sitl-tagged.ulg").metadata)
    flight.signals = {"att.roll": s}
    r = build_replay(flight)
    assert r.meta["sources"]["attitude"] is None
