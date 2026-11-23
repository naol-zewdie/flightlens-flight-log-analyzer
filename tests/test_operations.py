"""Operations: metrics, health, structured logs, job limits, resume and graceful shutdown."""

from __future__ import annotations

import io
import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from flightlens import cli
from flightlens.api import create_app
from flightlens.ops import JsonFormatter, Metrics, TextFormatter, request_id, setup_logging
from flightlens.store import Store
from flightlens.worker import MAX_ATTEMPTS, JobRunner

LOG = Path(__file__).parent.parent / "testdata" / "logs" / "px4-v1.13-sitl-tagged.ulg"


def _slow_child(log_path: str, out_dir: str, profiles: Any, conn: Any) -> None:
    """Stands in for the analysis process: never finishes on its own."""
    time.sleep(120)


def wait_for(predicate: Any, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition not reached")


def queue_log(store: Store, tmp: Path) -> tuple[str, str]:
    """Put a log in the library and queue it, as an upload does."""
    upload = tmp / "upload.ulg"
    shutil.copy(LOG, upload)
    import hashlib

    created = store.create_flight(
        upload, hashlib.sha256(LOG.read_bytes()).hexdigest(), LOG.name, "ulog"
    )
    assert created is not None
    return created


# --- metrics and logs ------------------------------------------------------------------------


def test_prometheus_text_format() -> None:
    m = Metrics()
    m.describe("demo_total", "counter", "Demo.")
    m.inc("demo_total", {"route": '/a"b'})
    m.inc("demo_total", {"route": '/a"b'}, 2)
    for v in (0.003, 0.2, 99):
        m.observe("demo_seconds", v, buckets=(0.01, 1.0))
    text = m.render([("demo_up", "Up.", [((), 1)])])
    assert "# TYPE demo_up gauge\ndemo_up 1" in text
    assert "# TYPE demo_total counter" in text
    assert 'demo_total{route="/a\\"b"} 3' in text  # label value escaped
    assert 'demo_seconds_bucket{le="0.01"} 1' in text
    assert 'demo_seconds_bucket{le="1"} 2' in text  # cumulative
    assert 'demo_seconds_bucket{le="+Inf"} 3' in text
    assert "demo_seconds_count 3" in text and "demo_seconds_sum 99.203" in text


def test_log_formats() -> None:
    record = logging.LogRecord("flightlens.jobs", logging.INFO, "", 0, "job done", None, None)
    record.flight = "3f2a"
    record.seconds = 4.2
    entry = json.loads(JsonFormatter().format(record))
    assert entry["msg"] == "job done" and entry["level"] == "info"
    assert entry["flight"] == "3f2a" and entry["seconds"] == 4.2 and entry["ts"].endswith("Z")
    assert TextFormatter().format(record).endswith("job done  flight=3f2a seconds=4.2")
    with pytest.raises(ValueError):
        setup_logging("xml")
    with pytest.raises(ValueError):
        setup_logging("json", "loud")


def test_request_ids() -> None:
    assert request_id("abc-123") == "abc-123"
    for hostile in ("a b", "x" * 65, "line\nbreak", "", None):
        rid = request_id(hostile)
        assert rid != hostile and len(rid) == 16


def _reset_logging() -> None:
    """Undo setup_logging, so later tests see the default logging set-up."""
    for name in ("flightlens", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    logging.getLogger("uvicorn.access").disabled = False


# --- the server ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path / "data"), base_url="http://localhost") as c:
        yield c


def test_health_metrics_and_request_ids(client: TestClient) -> None:
    stream = io.StringIO()
    setup_logging("json", "info", stream=stream)
    try:
        health = client.get("/api/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok" and all(body["checks"].values())
        assert body["workers"] == 1 and body["disk_free_bytes"] > 0

        r = client.get("/api/flights/nope", headers={"X-Request-ID": "trace-42"})
        assert r.status_code == 404 and r.headers["x-request-id"] == "trace-42"
        assert len(client.get("/api/flights").headers["x-request-id"]) == 16

        with LOG.open("rb") as fh:
            up = client.post("/api/logs", files={"file": (LOG.name, fh)}).json()
        wait_for(lambda: client.get(f"/api/jobs/{up['job_id']}").json()["status"] == "done")
        with LOG.open("rb") as fh:
            assert client.post("/api/logs", files={"file": (LOG.name, fh)}).json()["duplicate"]

        text = client.get("/metrics").text
    finally:
        _reset_logging()
    assert 'flightlens_http_requests_total{method="GET",route="/api/flights/{flight_id}",' in text
    assert 'status="404"} 1' in text
    assert "nope" not in text  # templates, never raw paths
    assert 'flightlens_uploads_total{result="queued"} 1' in text
    assert 'flightlens_uploads_total{result="duplicate"} 1' in text
    assert 'flightlens_jobs_total{outcome="done"} 1' in text
    assert "flightlens_job_duration_seconds_count 1" in text
    assert 'flightlens_flights{status="done"} 1' in text
    assert 'flightlens_flights_by_verdict{verdict="insufficient data"} 1' in text
    assert "flightlens_jobs_running 0" in text

    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    requests = [e for e in lines if e["msg"] == "request"]
    assert any(e["request_id"] == "trace-42" and e["status"] == 404 for e in requests)
    assert not any(e["path"] in ("/api/health", "/metrics") for e in requests)  # debug only
    done = next(e for e in lines if e["msg"] == "job done")
    assert done["flight"] == up["flight_id"] and done["verdict"] == "insufficient data"


def test_full_queue_and_low_disk_refuse_uploads(tmp_path: Path) -> None:
    app = create_app(tmp_path / "a", max_queue=0)
    with TestClient(app, base_url="http://localhost") as c, LOG.open("rb") as fh:
        r = c.post("/api/logs", files={"file": (LOG.name, fh)})
        assert r.status_code == 503 and r.headers["retry-after"] == "30"
        assert "queue is full" in r.json()["detail"]
        assert 'flightlens_uploads_total{result="refused_503"} 1' in c.get("/metrics").text

    app = create_app(tmp_path / "b", min_free_bytes=1 << 62)
    with TestClient(app, base_url="http://localhost") as c, LOG.open("rb") as fh:
        assert c.post("/api/logs", files={"file": (LOG.name, fh)}).status_code == 507
        health = c.get("/api/health")
        assert health.status_code == 200  # reads still work
        assert health.json()["status"] == "degraded" and not health.json()["checks"]["disk"]


def test_health_is_down_once_the_workers_stop(tmp_path: Path) -> None:
    app = create_app(tmp_path / "data")
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/api/health").status_code == 200
    # the lifespan has ended: the job workers are stopped
    r = TestClient(app, base_url="http://localhost").get("/api/health")
    assert r.status_code == 503 and r.json()["status"] == "down"


# --- jobs across restarts --------------------------------------------------------------------


def test_graceful_stop_requeues_and_restart_resumes(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    flight_id, job_id = queue_log(store, tmp_path)

    runner = JobRunner(store, workers=1)
    runner._target = _slow_child
    runner.start()
    wait_for(lambda: (store.get_job(job_id) or {})["status"] == "processing")
    assert runner.stop(grace_s=0.5) == {"interrupted": 1, "left_queued": 0}
    job = store.get_job(job_id)
    assert job is not None and job["status"] == "queued" and job["attempts"] == 1

    # the next start (a new server on the same library) finishes it
    again = Store(tmp_path / "data")
    resumed = JobRunner(again)
    assert resumed.start() == 1
    wait_for(lambda: (again.get_job(job_id) or {})["status"] == "done")
    assert again.get_flight(flight_id)["verdict"] == "insufficient data"  # type: ignore[index]
    assert again.get_job(job_id)["attempts"] == 2  # type: ignore[index]
    resumed.stop(grace_s=0)


def test_a_job_that_keeps_stopping_the_server_is_given_up(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    flight_id, job_id = queue_log(store, tmp_path)
    db = sqlite3.connect(tmp_path / "data" / "flightlens.db")
    db.execute("UPDATE jobs SET status='processing', attempts=? WHERE id=?", (MAX_ATTEMPTS, job_id))
    db.commit()
    resumed, given_up = Store(tmp_path / "data").resume_jobs(MAX_ATTEMPTS)
    assert resumed == [] and given_up == 1
    flight = store.get_flight(flight_id)
    assert flight is not None and flight["status"] == "failed"
    assert "interrupted 3 times" in flight["error"]


def test_opening_the_library_leaves_running_jobs_alone(tmp_path: Path) -> None:
    """The CLI (backup, prune, ...) opens the library while a server works on it."""
    store = Store(tmp_path / "data")
    _, job_id = queue_log(store, tmp_path)
    store.set_job(job_id, "processing")
    Store(tmp_path / "data")
    assert store.get_job(job_id)["status"] == "processing"  # type: ignore[index]


def test_parallel_workers_never_analyze_one_flight_twice_at_once(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    flight_id, first = queue_log(store, tmp_path)
    second = store.requeue(flight_id)
    runner = JobRunner(store, workers=2)
    runner.start()
    runner.submit(first, flight_id)
    runner.submit(second, flight_id)
    seen: set[int] = set()

    def both_done() -> bool:
        seen.add(runner.running())
        return all((store.get_job(j) or {})["status"] == "done" for j in (first, second))

    wait_for(both_done)
    assert max(seen) <= 1  # the second waited for the first
    runner.stop(grace_s=0)


# --- serve options ---------------------------------------------------------------------------


def test_serve_rejects_unknown_log_format(tmp_path: Path) -> None:
    r = CliRunner().invoke(
        cli.app, ["serve", "--data", str(tmp_path), "--log-format", "xml", "--port", "0"]
    )
    assert r.exit_code == cli.EXIT_USAGE and "log format" in r.output
