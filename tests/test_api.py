"""API tests: upload a real log, wait for the isolated worker, read results."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from flightlens.api import create_app
from flightlens.lttb import lttb, with_gaps

LOGS = Path(__file__).parent.parent / "testdata" / "logs"
REAL = Path(__file__).parent.parent / "testdata" / "real"
LOG = LOGS / "px4-v1.13-sitl-tagged.ulg"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(tmp_path / "data", timeout_s=120)
    with TestClient(app, base_url="http://localhost:8000") as c:
        yield c


def wait(client: TestClient, job_id: str, timeout: float = 120) -> dict:  # type: ignore[type-arg]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job  # type: ignore[no-any-return]
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def upload(client: TestClient, path: Path, name: str | None = None) -> dict:  # type: ignore[type-arg]
    with path.open("rb") as fh:
        r = client.post("/api/logs", files={"file": (name or path.name, fh)})
    assert r.status_code == 202, r.text
    return r.json()  # type: ignore[no-any-return]


def test_upload_analyze_and_read_back(client: TestClient) -> None:
    res = upload(client, LOG)
    assert wait(client, res["job_id"])["status"] == "done"
    fid = res["flight_id"]

    flight = client.get(f"/api/flights/{fid}").json()
    assert flight["flight"]["verdict"] == "insufficient data"  # never armed
    assert flight["report"]["source"]["sha256"].startswith(fid)

    assert client.get(f"/api/flights/{fid}/findings").json()["findings"]
    sigs = client.get(f"/api/flights/{fid}/signals?names=att.roll,nope&max_points=200").json()
    roll, missing = sigs["signals"]
    assert roll["unit"] == "deg" and 0 < len(roll["t"]) <= 210
    assert missing == {"name": "nope", "missing": True}

    listed = client.get("/api/flights").json()["flights"]
    assert [f["id"] for f in listed] == [fid]

    # Same bytes again: a duplicate, not a second flight.
    again = upload(client, LOG, name="renamed.ulg")
    assert again["duplicate"] is True and again["flight_id"] == fid

    original = client.get(f"/api/flights/{fid}/original")
    assert original.content == LOG.read_bytes()

    assert client.delete(f"/api/flights/{fid}").status_code == 200
    assert client.get(f"/api/flights/{fid}").status_code == 404


def test_rejects_non_logs_and_empty(client: TestClient, tmp_path: Path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    with txt.open("rb") as fh:
        assert client.post("/api/logs", files={"file": ("notes.ulg", fh)}).status_code == 415
    empty = tmp_path / "empty.ulg"
    empty.write_bytes(b"")
    with empty.open("rb") as fh:
        assert client.post("/api/logs", files={"file": ("empty.ulg", fh)}).status_code == 400


def test_corrupt_log_fails_cleanly(client: TestClient, tmp_path: Path) -> None:
    bad = tmp_path / "bad.ulg"
    rng = np.random.default_rng(1)
    bad.write_bytes(LOG.read_bytes()[:3000] + rng.bytes(20_000))
    res = upload(client, bad)
    job = wait(client, res["job_id"])
    assert job["status"] == "failed"
    assert job["error"]
    assert client.get("/api/health").json()["status"] == "ok"  # server still fine


@pytest.mark.skipif(
    not (REAL / "9c90f86e-e93f-4a51-82b0-fb896cda3106.ulg").exists(),
    reason="real log not downloaded",
)
def test_real_vibrating_hexacopter(client: TestClient) -> None:
    res = upload(client, REAL / "9c90f86e-e93f-4a51-82b0-fb896cda3106.ulg")
    assert wait(client, res["job_id"])["status"] == "done"
    fid = res["flight_id"]
    flight = client.get(f"/api/flights/{fid}").json()["flight"]
    assert flight["verdict"] == "problem"
    titles = [f["title"] for f in client.get(f"/api/flights/{fid}/findings").json()["findings"]]
    assert "Accelerometer clipping" in titles
    track = client.get(f"/api/flights/{fid}/track").json()
    assert track["source"] and len(track["lat"]) > 100


def test_unicode_and_odd_filenames(client: TestClient) -> None:
    res = upload(client, LOG, name='飞行 "x".ul:g')
    assert wait(client, res["job_id"])["status"] == "done"
    fid = res["flight_id"]
    r = client.get(f"/api/flights/{fid}/report.json")
    assert r.status_code == 200
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    stored = client.app.state.store.original_path(fid)  # type: ignore[attr-defined]
    assert stored.name == "original.log"


def test_refuses_foreign_host_and_cross_site_posts(client: TestClient) -> None:
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    with LOG.open("rb") as fh:
        r = client.post(
            "/api/logs", files={"file": ("a.ulg", fh)}, headers={"origin": "https://evil.example"}
        )
    assert r.status_code == 403
    with LOG.open("rb") as fh:
        r = client.post(
            "/api/logs", files={"file": ("a.ulg", fh)}, headers={"origin": "http://localhost:8000"}
        )
    assert r.status_code == 202


def test_rejects_oversized_upload_before_reading(client: TestClient) -> None:
    r = client.post(
        "/api/logs",
        content=b"x",
        headers={"content-length": str(2 * 1024**3), "content-type": "multipart/form-data"},
    )
    assert r.status_code == 413


def test_delete_refused_while_processing(client: TestClient) -> None:
    res = upload(client, LOG)
    store = client.app.state.store  # type: ignore[attr-defined]
    store.set_job(res["job_id"], "processing")
    assert client.delete(f"/api/flights/{res['flight_id']}").status_code == 409


def test_concurrent_duplicate_upload_is_claimed_once(tmp_path: Path) -> None:
    from flightlens.store import Store

    store = Store(tmp_path / "d")
    a, b = tmp_path / "a.ulg", tmp_path / "b.ulg"
    a.write_bytes(LOG.read_bytes())
    b.write_bytes(LOG.read_bytes())
    assert store.create_flight(a, "f" * 64, "a.ulg", "ulog") is not None
    assert store.create_flight(b, "f" * 64, "b.ulg", "ulog") is None
    assert b.exists()  # the losing upload was not moved over the original


def test_lttb_keeps_extremes_and_gaps() -> None:
    t = np.linspace(0, 100, 100_000)
    v = np.sin(t)
    v[50_000] = 10.0  # a spike must survive downsampling
    tt, vv = lttb(t, v, 500)
    assert tt.size == 500 and vv.max() == 10.0
    ts, vs = with_gaps(np.array([0.0, 1.0, 2.0, 10.0, 11.0]), np.array([1.0, 2.0, 3.0, 4.0, 5.0]))
    assert None in vs and len(ts) == 6


def test_upload_ardupilot_bin(client: TestClient, tmp_path: Path) -> None:
    from .test_ardupilot import copter_log

    res = upload(client, copter_log(tmp_path / "00000042.BIN"))
    assert wait(client, res["job_id"])["status"] == "done"
    flight = client.get(f"/api/flights/{res['flight_id']}").json()["flight"]
    assert flight["log_format"] == "dataflash"
    assert flight["verdict"] == "healthy"
    assert flight["firmware"].startswith("ArduCopter V4.5.7")
    track = client.get(f"/api/flights/{res['flight_id']}/track").json()
    assert len(track["lat"]) > 100


def test_spectrum_endpoint(client: TestClient, tmp_path: Path) -> None:
    from flightlens.sim.benchmark import Fault, copter

    res = upload(client, copter(tmp_path / "noise.bin", Fault("gyro_noise", level=12.0), 7))
    assert wait(client, res["job_id"])["status"] == "done"
    spec = client.get(f"/api/flights/{res['flight_id']}/spectrum").json()["spectrum"]
    assert spec["fs"] == 2000 and spec["tracks_throttle"] is True
    assert len(spec["freqs"]) == len(spec["psd_db"]["x"])
    grid = spec["spectrogram"]
    assert len(grid["db"]) == len(grid["t"]) and len(grid["db"][0]) == len(grid["f"])

    quiet = upload(client, LOG)  # never armed: no spectrum, but not an error
    assert wait(client, quiet["job_id"])["status"] == "done"
    assert client.get(f"/api/flights/{quiet['flight_id']}/spectrum").json() == {"spectrum": None}
