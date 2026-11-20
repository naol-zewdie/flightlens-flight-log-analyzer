"""Aircraft grouping, per-flight metrics and drift against an aircraft's own history."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightlens import parsers
from flightlens.api import create_app
from flightlens.demo import seed
from flightlens.engine import analyze
from flightlens.fleet import aircraft_key, assess
from flightlens.metrics import BY_KEY, flight_metrics
from flightlens.parsers.ardupilot import _hardware
from flightlens.sim.plane import plane_log
from flightlens.store import Store

QUAD = [f"quad-0{i}-healthy.bin" for i in range(1, 5)]
QUAD_VIB = "quad-10-vibration.bin"


# --- drift rule --------------------------------------------------------------------


def test_drift_needs_history_and_a_real_change() -> None:
    vib = BY_KEY["vibration"]  # higher is worse; at least +30% and +3 m/s²
    flags = [f for _, f in assess(vib, [15, 16, 15, 30])]
    assert flags == [False, False, False, True]
    assert [f for _, f in assess(vib, [15, 30])] == [False, False]  # too little history
    assert not assess(vib, [15, 16, 15, 17])[-1][1]  # within the usual scatter
    assert not assess(vib, [15, 16, 15, 5])[-1][1]  # better is never drift


def test_noisy_history_widens_the_limit() -> None:
    vib = BY_KEY["vibration"]
    assert assess(vib, [10, 30, 12, 28, 11, 30, 34])[-1] == (20, False)  # within 3 robust sigma
    assert assess(vib, [10, 11, 10, 12, 11, 10, 34])[-1][1]


def test_lower_is_worse_metrics() -> None:
    cell = BY_KEY["cell_min"]
    assert assess(cell, [3.6, 3.62, 3.61, 3.3])[-1][1]
    assert not assess(cell, [3.6, 3.62, 3.61, 3.9])[-1][1]


def test_aircraft_key_prefers_the_label() -> None:
    assert aircraft_key({"aircraft": "Quad-7", "serial": "AB"}) == "label:Quad-7"
    assert aircraft_key({"aircraft": None, "serial": "AB"}) == "sn:AB"
    assert aircraft_key({"aircraft": None, "serial": None}) is None


# --- identity and metrics ------------------------------------------------------------


def test_ardupilot_board_uid_in_both_formats() -> None:
    assert _hardware([(0, "CubeOrange 0040003A3138511133383538")]) == (
        "CubeOrange",
        "0040003A3138511133383538",
    )
    assert _hardware([(0, "CUAVv5-bdshot 00270035 3156500F 20333543")]) == (
        "CUAVv5-bdshot",
        "002700353156500F20333543",
    )
    assert _hardware([(0, "ArduPlane V4.6.2 (1ebd4d99)")]) == (None, None)


def test_plane_metrics(tmp_path: Path) -> None:
    flight = parsers.load(plane_log(tmp_path / "p.bin", "smooth", trim=-0.2))
    assert flight.metadata.serial == "0039002A3438510F37383539"
    m = flight_metrics(flight, analyze(flight))
    assert m["trim"] == pytest.approx(20, abs=1)
    assert m["airborne_min"] == pytest.approx(80 / 60, abs=0.05)
    assert {"vibration", "cell_min", "ekf", "score"} <= set(m)
    assert "motor_output" not in m  # no lift motors: left out, never 0


# --- store and API -------------------------------------------------------------------


def test_old_database_is_migrated(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "flightlens.db")
    db.executescript(
        "CREATE TABLE flights (id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, filename TEXT "
        "NOT NULL, log_format TEXT, size_bytes INTEGER, uploaded_at REAL NOT NULL, status TEXT "
        "NOT NULL, error TEXT, firmware TEXT, vehicle_type TEXT, hardware TEXT, start_time TEXT, "
        "duration_s REAL, verdict TEXT, score INTEGER, n_problem INTEGER, n_attention INTEGER, "
        "aircraft TEXT);"
        "INSERT INTO flights (id, sha256, filename, uploaded_at, status) "
        "VALUES ('a', 'aa', 'old.ulg', 1, 'done');"
    )
    db.commit()
    db.close()
    store = Store(tmp_path)
    assert store.get_flight("a")["serial"] is None  # type: ignore[index]
    assert store.stale_flights() == ["a"]


@pytest.fixture(scope="module")
def fleet_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("fleet")
    seed(data, only=[*QUAD, QUAD_VIB])
    return data


@pytest.fixture
def client(fleet_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(fleet_dir), base_url="http://localhost") as c:
        yield c


def test_fleet_groups_by_board_and_flags_drift(client: TestClient) -> None:
    listing = client.get("/api/aircraft").json()
    assert listing["unassigned"] == 0 and listing["stale"] == 0
    (quad,) = listing["aircraft"]
    assert quad["hardware"] == "CubeOrange" and quad["flights"] == 5
    assert "Vibration has drifted" in [a["title"] for a in quad["alerts"]]

    detail = client.get(f"/api/aircraft/{quad['key']}").json()
    assert [f["filename"] for f in detail["flight_list"]] == [*QUAD, QUAD_VIB]  # by log date
    vib = next(m for m in detail["metrics"] if m["key"] == "vibration")
    assert [p["flagged"] for p in vib["points"]] == [False, False, False, False, True]
    assert client.get("/api/aircraft/sn:nope").status_code == 404


def test_naming_an_aircraft_names_its_board(client: TestClient, fleet_dir: Path) -> None:
    flights = client.get("/api/flights").json()["flights"]
    r = client.patch(
        f"/api/flights/{flights[0]['id']}", json={"aircraft": "Quad-7", "same_board": True}
    )
    assert r.json()["updated"] == 5
    keys = [a["key"] for a in client.get("/api/aircraft").json()["aircraft"]]
    assert keys == ["label:Quad-7"]
    # a new flight from the same board joins the named aircraft by itself
    seed(fleet_dir, only=["quad-05-gps-loss.bin"])
    new = Store(fleet_dir).list_flights()
    assert {f["aircraft"] for f in new} == {"Quad-7"}
