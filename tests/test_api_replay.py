"""Replay over the API and the CLI: metadata, the binary table, outdated marking."""

from __future__ import annotations

import gzip
import json
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from flightlens import cli
from flightlens.api import create_app
from flightlens.replay import read_columns
from flightlens.sim.manoeuvre import manoeuvre_log


@pytest.fixture(scope="module")
def log(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return manoeuvre_log(tmp_path_factory.mktemp("m") / "manoeuvres.bin")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path / "data"), base_url="http://localhost") as c:
        yield c


def analyzed(client: TestClient, log: Path) -> str:
    with log.open("rb") as fh:
        up = client.post("/api/logs", files={"file": (log.name, fh)}).json()
    deadline = time.time() + 180
    while time.time() < deadline:
        if client.get(f"/api/jobs/{up['job_id']}").json()["status"] in ("done", "failed"):
            break
        time.sleep(0.2)
    assert client.get(f"/api/jobs/{up['job_id']}").json()["status"] == "done"
    return str(up["flight_id"])


def test_replay_metadata_and_table(client: TestClient, log: Path) -> None:
    fid = analyzed(client, log)
    meta = client.get(f"/api/flights/{fid}/replay").json()
    assert meta["sources"]["attitude"] == "roll, pitch and yaw"
    assert meta["origin"]["chosen"] == "take-off point" and meta["rate_hz"] == 25

    r = client.get(f"/api/flights/{fid}/replay.bin", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["x-replay-rows"] == str(meta["rows"])
    # httpx decodes gzip transparently; the server did compress
    assert r.headers.get("content-encoding") == "gzip"
    data = np.frombuffer(r.content, dtype="<f4")
    assert data.size == meta["rows"] * len(meta["columns"])
    table = data.reshape(len(meta["columns"]), meta["rows"])
    stored = read_columns(client.app.state.store.flight_dir(fid) / "analysis")  # type: ignore[attr-defined]
    assert stored is not None
    for i, name in enumerate(meta["columns"]):
        assert np.allclose(table[i], stored[name].astype("<f4"), equal_nan=True), name
    east = table[meta["columns"].index("east")]
    assert 19.5 < np.nanmax(east) < 20.5  # the 20 m square

    raw = client.get(f"/api/flights/{fid}/replay.bin", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers and raw.content == r.content
    assert len(gzip.compress(raw.content)) < len(raw.content)


def test_flight_analyzed_before_replay_is_outdated(client: TestClient, log: Path) -> None:
    fid = analyzed(client, log)
    assert client.get(f"/api/flights/{fid}").json()["stale"] == []
    store = client.app.state.store  # type: ignore[attr-defined]
    analysis = store.flight_dir(fid) / "analysis"
    (analysis / "replay.json").unlink()
    (analysis / "replay.parquet").unlink()
    db = store._db
    versions = json.loads(
        db.execute("SELECT versions FROM flights WHERE id=?", (fid,)).fetchone()[0]
    )
    versions["artifacts"] = {}  # as a 1.0 analysis recorded it
    db.execute("UPDATE flights SET versions=? WHERE id=?", (json.dumps(versions), fid))
    db.commit()
    r = client.get(f"/api/flights/{fid}/replay")
    assert r.status_code == 409 and "re-analyze" in r.json()["detail"]
    assert client.get(f"/api/flights/{fid}/replay.bin").status_code == 409
    [entry] = client.get("/api/stale").json()["flights"]
    assert entry["id"] == fid and "analyzed before replay existed" in entry["reasons"]


def test_unknown_flight_and_cli(client: TestClient, log: Path) -> None:
    assert client.get("/api/flights/nope/replay").status_code == 404
    r = CliRunner().invoke(cli.app, ["replay-info", str(log)])
    assert r.exit_code == 0, r.output
    assert "25 Hz" in r.output and "take-off point" in r.output
    assert "roll, pitch and yaw" in r.output and "gaps      none" in r.output
