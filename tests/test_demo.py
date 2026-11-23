"""Demo seeding and the read-only server mode."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightlens.api import create_app
from flightlens.demo import DEMO, seed
from flightlens.store import Store

PICK = ["quad-07-gyro-noise.bin", "plane-04-crash.bin"]


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("demo")
    seed(data, only=PICK)
    return data


def test_seed_analyzes_and_is_idempotent(demo_dir: Path) -> None:
    rows = Store(demo_dir).list_flights()
    assert sorted(r["filename"] for r in rows) == sorted(PICK)
    assert {r["status"] for r in rows} == {"done"}
    verdicts = {r["filename"]: r["verdict"] for r in rows}
    assert verdicts == {"quad-07-gyro-noise.bin": "attention", "plane-04-crash.bin": "problem"}
    again = seed(demo_dir, only=PICK)  # same bytes: recognised, not duplicated
    assert len(again) == 2 and len(Store(demo_dir).list_flights()) == 2
    assert all(name.endswith((".bin", ".tlog")) for name in DEMO)


def test_demo_includes_a_telemetry_log(tmp_path: Path) -> None:
    seed(tmp_path, only=["quad-11-telemetry.tlog"])
    (row,) = Store(tmp_path).list_flights()
    assert row["log_format"] == "tlog" and row["status"] == "done"
    assert row["verdict"] == "attention"  # the vibration it was generated with
    assert row["serial"] == "0040003A3138511133383538"  # joins the demo quadcopter


@pytest.fixture
def ro_client(demo_dir: Path) -> Iterator[TestClient]:
    app = create_app(demo_dir, read_only=True)
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def test_read_only_server_serves_but_refuses_changes(ro_client: TestClient) -> None:
    assert ro_client.get("/api/health").json()["read_only"] is True
    flights = ro_client.get("/api/flights").json()["flights"]
    fid = flights[0]["id"]
    assert ro_client.get(f"/api/flights/{fid}").status_code == 200
    refused = [
        ro_client.post("/api/logs", files={"file": ("x.bin", b"abc")}),
        ro_client.delete(f"/api/flights/{fid}"),
        ro_client.post(f"/api/flights/{fid}/reanalyze"),
        ro_client.patch(f"/api/flights/{fid}", json={"aircraft": "x"}),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403, 403]
    assert "read-only" in refused[0].json()["detail"]
    assert len(ro_client.get("/api/flights").json()["flights"]) == len(flights)
