"""Phase 4: stale-analysis detection, engineer reviews, HTML reports."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from flightlens import cli, parsers, validate
from flightlens.api import create_app
from flightlens.demo import seed
from flightlens.engine import analyze
from flightlens.htmlreport import flight_reader, fmt_num, fmt_time, render_html
from flightlens.provenance import current_versions, stale_reasons, versions_of
from flightlens.reviews import finding_key, manifest
from flightlens.sim.plane import plane_log
from flightlens.store import Store

VIB = "quad-10-vibration.bin"
HEALTHY = "quad-01-healthy.bin"


# --- provenance ----------------------------------------------------------------------------


def _current(fmt: str = "dataflash") -> dict[str, Any]:
    now = current_versions()
    return {"checks": dict(now["checks"]), "mapping": now["mappings"][fmt], "format": fmt}


def test_current_analysis_is_not_stale() -> None:
    assert stale_reasons(_current()) == []
    assert stale_reasons(_current("ulog")) == []


def test_changed_check_or_mapping_is_stale() -> None:
    v = _current()
    v["checks"]["vibration.level"] = "0.9.0"
    del v["checks"]["control.trim"]
    v["mapping"] = "ardupilot-2020.1.1"
    reasons = stale_reasons(v)
    now = current_versions()
    assert f"vibration.level 0.9.0 → {now['checks']['vibration.level']}" in reasons
    assert "new check control.trim" in reasons
    assert any(r.startswith("signal mapping ardupilot-2020.1.1 →") for r in reasons)


def test_unknown_versions_and_missing_metrics_are_stale() -> None:
    assert stale_reasons(None) == ["the versions it was analyzed with are unknown"]
    assert stale_reasons(_current(), has_metrics=False) == ["analyzed before trend metrics existed"]


def test_versions_of_a_report(tmp_path: Path) -> None:
    flight = parsers.load(plane_log(tmp_path / "p.bin", "smooth"))
    v = versions_of(analyze(flight).to_dict())
    assert v["format"] == "dataflash" and v["mapping"].startswith("ardupilot-")
    assert v["checks"] == current_versions()["checks"]


# --- web app: stale, reviews, report ---------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    seed(tmp_path, only=[VIB, HEALTHY])
    return tmp_path


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(data_dir), base_url="http://localhost") as c:
        yield c


def _id(client: TestClient, filename: str) -> str:
    flights = client.get("/api/flights").json()["flights"]
    return str(next(f["id"] for f in flights if f["filename"] == filename))


def _wait(client: TestClient, timeout: float = 120) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        flights = client.get("/api/flights").json()["flights"]
        if all(f["status"] in ("done", "failed") for f in flights):
            return
        time.sleep(0.3)
    raise AssertionError("analysis did not finish")


def _age(data_dir: Path, flight_id: str) -> None:
    """Pretend a flight was analyzed by an older vibration check."""
    db = sqlite3.connect(data_dir / "flightlens.db")
    (versions,) = db.execute("SELECT versions FROM flights WHERE id=?", (flight_id,)).fetchone()
    v = json.loads(versions)
    v["checks"]["vibration.level"] = "0.0.1"
    db.execute("UPDATE flights SET versions=? WHERE id=?", (json.dumps(v), flight_id))
    db.commit()
    db.close()


def test_fresh_analyses_are_current(client: TestClient) -> None:
    assert client.get("/api/stale").json()["flights"] == []
    assert not any(f["stale"] for f in client.get("/api/flights").json()["flights"])
    fid = _id(client, VIB)
    assert client.get(f"/api/flights/{fid}").json()["stale"] == []


def test_outdated_flight_is_flagged_and_reanalyzed(client: TestClient, data_dir: Path) -> None:
    fid = _id(client, VIB)
    _age(data_dir, fid)
    (entry,) = client.get("/api/stale").json()["flights"]
    assert entry["id"] == fid and entry["reasons"][0].startswith("vibration.level 0.0.1 →")
    assert client.get(f"/api/flights/{fid}").json()["stale"] == entry["reasons"]
    assert client.post("/api/reanalyze-stale").json() == {"queued": 1}
    _wait(client)
    assert client.get("/api/stale").json()["flights"] == []


def test_versions_are_backfilled_from_old_reports(data_dir: Path) -> None:
    db = sqlite3.connect(data_dir / "flightlens.db")
    db.execute("UPDATE flights SET versions=NULL")
    db.commit()
    db.close()
    store = Store(data_dir)  # migration reads them back from report.json
    assert store.stale_details() == {}
    assert all(f["versions"] for f in store.list_flights())


def test_review_a_finding(client: TestClient) -> None:
    fid = _id(client, VIB)
    findings = client.get(f"/api/flights/{fid}/findings").json()
    vib = next(f for f in findings["findings"] if f["check_id"] == "vibration.level")
    assert vib["key"] == finding_key(vib) and vib["key"].startswith("vibration.level@")
    assert vib["review"] is None and findings["orphaned_reviews"] == []

    url = f"/api/flights/{fid}/reviews/{vib['key']}"
    assert client.put(url, json={"status": "maybe"}).status_code == 422
    r = client.put(url, json={"status": "confirmed", "note": "  prop chipped  "})
    assert r.status_code == 200 and r.json()["review"]["note"] == "prop chipped"
    again = client.get(f"/api/flights/{fid}/findings").json()["findings"]
    assert next(f for f in again if f["key"] == vib["key"])["review"]["status"] == "confirmed"
    assert (
        next(f for f in client.get("/api/flights").json()["flights"] if f["id"] == fid)["reviewed"]
        == 1
    )

    ok = next(f for f in again if f["severity"] == "ok")
    assert (
        client.put(
            f"/api/flights/{fid}/reviews/{ok['key']}", json={"status": "confirmed"}
        ).status_code
        == 422
    )
    assert (
        client.put(f"/api/flights/{fid}/reviews/nope@1", json={"status": "confirmed"}).status_code
        == 404
    )

    # the review survives re-analysis of the same log
    client.post(f"/api/flights/{fid}/reanalyze")
    _wait(client)
    after = client.get(f"/api/flights/{fid}/findings").json()["findings"]
    assert next(f for f in after if f["key"] == vib["key"])["review"]["status"] == "confirmed"

    assert client.delete(url).status_code == 200
    assert client.delete(url).status_code == 404


def test_reviews_export_as_validation_labels(
    client: TestClient, data_dir: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    fid = _id(client, VIB)
    findings = client.get(f"/api/flights/{fid}/findings").json()["findings"]
    issues = [f for f in findings if f["severity"] in ("problem", "attention")]
    vib = next(f for f in issues if f["check_id"] == "vibration.level")
    client.put(f"/api/flights/{fid}/reviews/{vib['key']}", json={"status": "confirmed"})

    out = tmp_path_factory.mktemp("labels") / "reviews.yaml"
    r = client.get("/api/reviews/manifest.yaml")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    out.write_text(r.text, encoding="utf-8")

    # confirmed: the current version still finds it, so the label passes
    (case,) = validate.load_manifest(out)
    assert case.path.is_file() and [str(rule) for rule in case.expect] == [
        f"vibration.level:{vib['severity']}"
    ]
    assert validate.evaluate(case).passed

    # marked a false alarm instead: the same finding now fails validation
    client.put(f"/api/flights/{fid}/reviews/{vib['key']}", json={"status": "false_alarm"})
    out.write_text(client.get("/api/reviews/manifest.yaml").text, encoding="utf-8")
    (case,) = validate.load_manifest(out)
    outcome = validate.evaluate(case)
    assert not outcome.passed and [f.check_id for f in outcome.false_alarms] == ["vibration.level"]

    # the CLI exports the same file
    cli_out = out.with_name("cli.yaml")
    res = CliRunner().invoke(
        cli.app, ["reviews", "export", "--data", str(data_dir), "--out", str(cli_out)]
    )
    assert res.exit_code == 0, res.output
    assert validate.load_manifest(cli_out)[0].must_not


def test_manifest_without_window_uses_short_rule(tmp_path: Path) -> None:
    rows = [
        {
            "flight_id": "abc",
            "original": "original.bin",
            "filename": "f.bin",
            "check_id": "control.trim",
            "severity": "attention",
            "t_start": None,
            "t_end": None,
            "status": "accepted",
        }
    ]
    m = manifest(rows, tmp_path)
    assert m["logs"]["abc/original.bin"]["expect"] == ["control.trim:attention"]


def test_deleting_a_flight_deletes_its_reviews(client: TestClient, data_dir: Path) -> None:
    fid = _id(client, VIB)
    vib = next(
        f
        for f in client.get(f"/api/flights/{fid}/findings").json()["findings"]
        if f["check_id"] == "vibration.level"
    )
    client.put(f"/api/flights/{fid}/reviews/{vib['key']}", json={"status": "accepted"})
    assert client.delete(f"/api/flights/{fid}").status_code == 200
    assert Store(data_dir).all_reviews() == []


def test_html_report_from_the_server(client: TestClient) -> None:
    fid = _id(client, VIB)
    vib = next(
        f
        for f in client.get(f"/api/flights/{fid}/findings").json()["findings"]
        if f["check_id"] == "vibration.level"
    )
    client.put(
        f"/api/flights/{fid}/reviews/{vib['key']}",
        json={"status": "confirmed", "note": "<b>prop</b> chipped"},
    )
    r = client.get(f"/api/flights/{fid}/report.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    html = r.text
    assert VIB in html and "Problem" in html and "<svg" in html
    assert "Engineer review: Confirmed" in html
    assert "&lt;b&gt;prop&lt;/b&gt; chipped" in html  # notes are escaped
    assert "http://" not in html and "https://" not in html  # nothing to fetch
    d = client.get(f"/api/flights/{fid}/report.html?download=1")
    assert "quad-10-vibration.report.html" in d.headers["content-disposition"]


def test_read_only_server_refuses_reviews(data_dir: Path) -> None:
    with TestClient(create_app(data_dir, read_only=True), base_url="http://localhost") as c:
        fid = _id(c, VIB)
        assert (
            c.put(f"/api/flights/{fid}/reviews/x@1", json={"status": "confirmed"}).status_code
            == 403
        )
        assert c.get(f"/api/flights/{fid}/report.html").status_code == 200


# --- HTML report from the command line ------------------------------------------------------


def test_html_report_escapes_and_charts(tmp_path: Path) -> None:
    flight = parsers.load(plane_log(tmp_path / "crash.bin", "crash", trim=-0.2))
    report = analyze(flight).to_dict()
    html = render_html(report, flight_reader(flight.signals), filename="<script>x</script>.bin")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html
    assert html.count("<svg") >= 2  # evidence charts
    assert "Crash" in html and report["source"]["sha256"] in html
    assert "check defaults" in html  # provenance: no profile


def test_cli_writes_html_report(tmp_path: Path) -> None:
    log = plane_log(tmp_path / "hard.bin", "hard")
    out = tmp_path / "hard.report.html"
    res = CliRunner().invoke(cli.app, ["analyze", str(log), "--html", str(out)])
    assert res.exit_code == 1, res.output  # attention: a hard landing
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_number_and_time_formats_match_the_web_app() -> None:
    assert [fmt_num(x) for x in (15869.2, 43.44, 3.521, 0.84, 120.0, 7.0)] == [
        "15,869",
        "43.4",
        "3.52",
        "0.84",
        "120",
        "7",
    ]
    assert [fmt_time(x) for x in (5, 65, 3725, -8)] == ["00:05", "01:05", "1:02:05", "−00:08"]
