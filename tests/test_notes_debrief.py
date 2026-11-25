"""Notes and debriefs: the API, read-only and cross-site refusals, cascade, prune."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightlens import retention
from flightlens.api import create_app
from flightlens.demo import seed
from flightlens.store import Store

PICK = ["quad-01-healthy.bin", "quad-05-gps-loss.bin"]


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("lib")
    seed(data, only=PICK)
    return data


@pytest.fixture
def client(library: Path, tmp_path: Path) -> Iterator[TestClient]:
    data = tmp_path / "data"
    shutil.copytree(library, data)
    with TestClient(create_app(data), base_url="http://localhost") as c:
        yield c


def ids(client: TestClient) -> dict[str, str]:
    return {f["filename"]: f["id"] for f in client.get("/api/flights").json()["flights"]}


def test_notes_crud(client: TestClient) -> None:
    fid = ids(client)[PICK[1]]
    r = client.post(
        f"/api/flights/{fid}/notes",
        json={"t_start": 72.5, "text": "  GPS drops here ", "tags": ["Sensor", "sensor", "gps "],
              "view": {"camera": "chase"}, "author": "Naol"},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    note = r.json()["note"]
    assert note["text"] == "GPS drops here" and note["tags"] == ["gps", "sensor"]
    assert note["view"] == {"camera": "chase"} and note["t_end"] is None
    rng = client.post(
        f"/api/flights/{fid}/notes", json={"t_start": 80, "t_end": 95, "text": "loiter"}
    )
    assert rng.status_code == 201

    listed = client.get(f"/api/flights/{fid}/notes").json()["notes"]
    assert [n["t_start"] for n in listed] == [72.5, 80]  # in time order

    r = client.patch(f"/api/notes/{note['id']}", json={"text": "GPS lost", "t_end": 85})
    assert r.status_code == 200 and r.json()["note"]["text"] == "GPS lost"
    assert r.json()["note"]["t_end"] == 85 and r.json()["note"]["tags"] == ["gps", "sensor"]
    for bad in ({"t_end": 10}, {"text": "   "}, {"t_start": None}):
        assert client.patch(f"/api/notes/{note['id']}", json=bad).status_code == 422, bad
    assert client.delete(f"/api/notes/{note['id']}").status_code == 200
    assert len(client.get(f"/api/flights/{fid}/notes").json()["notes"]) == 1
    assert client.delete(f"/api/notes/{note['id']}").status_code == 404


def test_note_validation(client: TestClient) -> None:
    fid = ids(client)[PICK[0]]
    url = f"/api/flights/{fid}/notes"
    for bad in (
        {"t_start": 10, "t_end": 5, "text": "x"},
        {"t_start": -1, "text": "x"},
        {"t_start": 1, "text": ""},
        {"t_start": 1, "text": "x" * 2001},
        {"t_start": 1, "text": "x", "tags": ["t" * 40]},
        {"t_start": 1, "text": "x", "view": {"blob": "y" * 3000}},
    ):
        assert client.post(url, json=bad).status_code == 422, bad
    assert (
        client.post("/api/flights/nope/notes", json={"t_start": 1, "text": "x"}).status_code == 404
    )


def test_debrief_and_library_badge(client: TestClient) -> None:
    names = ids(client)
    fid = names[PICK[1]]
    assert client.get(f"/api/flights/{fid}/debrief").json()["debrief"] is None
    body = {"status": "open", "summary": " GPS lost for 15 s in loiter. ",
            "actions": ["Move the GPS mast away from the VTX", "  "], "author": "Naol"}  # fmt: skip
    d = client.put(f"/api/flights/{fid}/debrief", json=body).json()["debrief"]
    assert d["status"] == "open" and d["summary"] == "GPS lost for 15 s in loiter."
    assert d["actions"] == ["Move the GPS mast away from the VTX"] and d["conclusion"] is None
    closed = {**body, "status": "closed", "conclusion": "Antenna placement."}
    d = client.put(f"/api/flights/{fid}/debrief", json=closed).json()["debrief"]
    assert d["status"] == "closed" and d["conclusion"] == "Antenna placement."
    assert client.put(f"/api/flights/{fid}/debrief", json={"status": "done"}).status_code == 422

    client.post(f"/api/flights/{fid}/notes", json={"t_start": 1, "text": "x"})
    rows = {f["filename"]: f for f in client.get("/api/flights").json()["flights"]}
    assert rows[PICK[1]]["debrief"] == "closed" and rows[PICK[1]]["notes"] == 1
    assert rows[PICK[0]]["debrief"] is None and rows[PICK[0]]["notes"] == 0

    def only(q: str) -> list[str]:
        found = client.get(f"/api/flights?debrief={q}").json()["flights"]
        return [f["filename"] for f in found]

    assert only("any") == only("closed") == [PICK[1]]
    assert only("none") == [PICK[0]] and only("open") == []


def test_writes_refused_read_only_and_cross_site(
    client: TestClient, library: Path, tmp_path: Path
) -> None:
    fid = ids(client)[PICK[0]]
    evil = {"Origin": "http://evil.example"}
    assert (
        client.post(
            f"/api/flights/{fid}/notes", json={"t_start": 1, "text": "x"}, headers=evil
        ).status_code
        == 403
    )
    assert (
        client.put(f"/api/flights/{fid}/debrief", json={"status": "open"}, headers=evil).status_code
        == 403
    )
    ro = tmp_path / "ro"
    shutil.copytree(library, ro)
    with TestClient(create_app(ro, read_only=True), base_url="http://localhost") as c:
        assert c.get(f"/api/flights/{fid}/notes").status_code == 200
        assert (
            c.post(f"/api/flights/{fid}/notes", json={"t_start": 1, "text": "x"}).status_code == 403
        )
        assert c.put(f"/api/flights/{fid}/debrief", json={"status": "open"}).status_code == 403


def test_deleting_a_flight_removes_its_notes_and_debrief(client: TestClient) -> None:
    fid = ids(client)[PICK[0]]
    note = client.post(f"/api/flights/{fid}/notes", json={"t_start": 1, "text": "x"}).json()["note"]
    client.put(f"/api/flights/{fid}/debrief", json={"status": "open"})
    assert client.delete(f"/api/flights/{fid}").status_code == 200
    store = client.app.state.store  # type: ignore[attr-defined]
    assert store.get_note(note["id"]) is None and store.get_debrief(fid) is None


def test_prune_keeps_annotated_and_debriefed_flights(library: Path, tmp_path: Path) -> None:
    data = tmp_path / "data"
    shutil.copytree(library, data)
    store = Store(data)
    rows = {r["filename"]: r["id"] for r in store.list_flights()}
    future = time.time() + 1e9
    assert len(retention.candidates(store, 1, now=future)) == 2
    store.add_note(rows[PICK[0]], {"t_start": 1.0, "text": "kept"})
    store.put_debrief(rows[PICK[1]], {"status": "closed"})
    assert retention.candidates(store, 1, now=future) == []
    assert len(retention.candidates(store, 1, keep_reviewed=False, now=future)) == 2
