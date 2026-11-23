"""Data safety: versioned schema migrations, backup and restore, retention."""

from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flightlens import backup, cli, retention, schema
from flightlens.demo import seed
from flightlens.store import Store

PICK = ["quad-01-healthy.bin", "quad-10-vibration.bin", "plane-04-crash.bin"]
runner = CliRunner()


# --- schema migrations -------------------------------------------------------------------


def test_new_library_gets_the_current_schema(tmp_path: Path) -> None:
    store = Store(tmp_path)
    assert (store.schema_from, store.schema_version) == (0, schema.SCHEMA_VERSION)
    db = sqlite3.connect(tmp_path / "flightlens.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == schema.SCHEMA_VERSION
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"flights", "jobs", "profiles", "aircraft_profiles", "reviews"} <= tables
    assert not list(tmp_path.glob("flightlens.db.before-*"))  # nothing to protect yet


def _v01_library(path: Path) -> None:
    """A database as FlightLens 0.1 left it: no schema version, fewer columns."""
    db = sqlite3.connect(path / "flightlens.db")
    db.executescript(
        "CREATE TABLE flights (id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, filename TEXT "
        "NOT NULL, log_format TEXT, size_bytes INTEGER, uploaded_at REAL NOT NULL, status TEXT "
        "NOT NULL, error TEXT, firmware TEXT, vehicle_type TEXT, hardware TEXT, start_time TEXT, "
        "duration_s REAL, verdict TEXT, score INTEGER, n_problem INTEGER, n_attention INTEGER, "
        "aircraft TEXT);"
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, flight_id TEXT NOT NULL, status TEXT NOT NULL, "
        "error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "INSERT INTO flights (id, sha256, filename, uploaded_at, status, verdict, aircraft) "
        "VALUES ('a1', 'aa11', 'old.ulg', 1, 'done', 'problem', 'Quad-7');"
    )
    db.commit()
    db.close()


def test_old_library_is_upgraded_and_kept(tmp_path: Path) -> None:
    _v01_library(tmp_path)
    store = Store(tmp_path)
    assert (store.schema_from, store.schema_version) == (0, schema.SCHEMA_VERSION)
    row = store.get_flight("a1")
    assert row is not None and row["verdict"] == "problem" and row["aircraft"] == "Quad-7"
    assert {"serial", "metrics", "versions"} <= set(row)
    copy = tmp_path / f"flightlens.db.before-v{schema.SCHEMA_VERSION}"
    assert copy.exists()
    old = sqlite3.connect(copy)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 0  # the copy is the old database
    again = Store(tmp_path)
    assert again.schema_from == schema.SCHEMA_VERSION  # opened, not migrated twice


def test_newer_library_is_refused(tmp_path: Path) -> None:
    Store(tmp_path)
    db = sqlite3.connect(tmp_path / "flightlens.db")
    db.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 1}")
    db.commit()
    db.close()
    with pytest.raises(schema.SchemaError, match="newer FlightLens"):
        Store(tmp_path)


def test_failed_migration_stops_at_the_last_complete_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(db: sqlite3.Connection, data: Path) -> None:
        db.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("disk full")

    steps = [*schema.MIGRATIONS[:2], (3, "broken", broken)]
    monkeypatch.setattr(schema, "MIGRATIONS", tuple(steps))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 3)
    db = sqlite3.connect(tmp_path / "flightlens.db")
    with pytest.raises(RuntimeError, match="disk full"):
        schema.migrate(db, tmp_path / "flightlens.db")
    assert schema.version(db) == 2
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "half_done" not in tables  # rolled back, not half-applied


def test_a_retried_upgrade_keeps_the_original_safety_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _v01_library(tmp_path)

    def broken(db: sqlite3.Connection, data: Path) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(schema, "MIGRATIONS", (*schema.MIGRATIONS[:2], (3, "broken", broken)))
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 3)
    for _ in range(2):  # the first attempt stops at version 2; the retry fails again
        db = sqlite3.connect(tmp_path / "flightlens.db")
        with pytest.raises(RuntimeError):
            schema.migrate(db, tmp_path / "flightlens.db")
        db.close()
    copy = sqlite3.connect(tmp_path / "flightlens.db.before-v3")
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 0  # still the original


# --- backup and restore ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data = tmp_path_factory.mktemp("lib")
    seed(data, only=PICK)
    store = Store(data)
    fid = next(r["id"] for r in store.list_flights() if r["filename"] == "quad-10-vibration.bin")
    report = store.read_report(fid)
    assert report is not None
    vib = next(f for f in report["findings"] if f["check_id"] == "vibration.level")
    store.set_review(fid, vib, "confirmed", "prop chipped")
    store.set_aircraft(fid, "Quad-7", same_board=True)
    return data


def _snapshot(data: Path) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    db = sqlite3.connect(data / "flightlens.db")
    flights = db.execute(
        "SELECT id, sha256, filename, verdict, score, aircraft, serial FROM flights ORDER BY id"
    ).fetchall()
    reviews = db.execute("SELECT flight_id, finding_key, status, note FROM reviews").fetchall()
    return flights, reviews


def test_backup_and_restore_round_trip(library: Path, tmp_path: Path) -> None:
    out = tmp_path / "lib.zip"
    summary = backup.create(library, out)
    assert summary.flights == 3 and summary.included_analysis and out.exists()
    restored = tmp_path / "restored"
    result = backup.restore(out, restored)
    assert result["flights"] == 3 and result["reanalyzed"] == 0
    assert _snapshot(restored) == _snapshot(library)
    for fid in (r[0] for r in _snapshot(library)[0]):
        a = json.loads((library / "flights" / str(fid) / "analysis" / "report.json").read_text())
        b = json.loads((restored / "flights" / str(fid) / "analysis" / "report.json").read_text())
        assert a["findings"] == b["findings"]


def test_backup_without_analysis_is_rebuilt_on_restore(library: Path, tmp_path: Path) -> None:
    full = backup.create(library, tmp_path / "full.zip")
    small = backup.create(library, tmp_path / "small.zip", include_analysis=False)
    assert small.size_bytes < full.size_bytes
    restored = tmp_path / "restored"
    seen: list[str] = []
    result = backup.restore(tmp_path / "small.zip", restored, progress=seen.append)
    assert result["reanalyzed"] == 3 and sorted(seen) == sorted(PICK)
    assert _snapshot(restored) == _snapshot(library)  # same verdicts: same FlightLens


def test_backup_is_consistent_while_the_library_is_open(library: Path, tmp_path: Path) -> None:
    store = Store(library)  # an open writer, as `flightlens serve` would be
    out = backup.create(library, tmp_path / "live.zip").path
    with zipfile.ZipFile(out) as z:
        snap = tmp_path / "snap.db"
        snap.write_bytes(z.read("flightlens.db"))
    assert sqlite3.connect(snap).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(store.list_flights()) == 3


def test_restore_refuses_to_overwrite(library: Path, tmp_path: Path) -> None:
    out = backup.create(library, tmp_path / "lib.zip").path
    target = tmp_path / "busy"
    target.mkdir()
    (target / "something").write_text("x")
    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore(out, target)
    assert (target / "something").read_text() == "x"


def _rewrite(src: Path, dst: Path, change: object) -> Path:
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, "w") as b:
        for info in a.infolist():
            name, data = change(info.filename, a.read(info))  # type: ignore[operator]
            if name is not None:
                b.writestr(name, data)
    return dst


def test_restore_refuses_damaged_or_hostile_backups(library: Path, tmp_path: Path) -> None:
    good = backup.create(library, tmp_path / "lib.zip").path
    flipped = {"done": False}

    def damage(name: str, data: bytes) -> tuple[str, bytes]:
        if "/original." in name and not flipped["done"]:
            flipped["done"] = True
            return name, data[:-1] + bytes([data[-1] ^ 1])
        return name, data

    def escape(name: str, data: bytes) -> tuple[str, bytes]:
        return ("../evil.txt" if name.endswith("report.json") else name), data

    def newer(name: str, data: bytes) -> tuple[str, bytes]:
        if name == "manifest.json":
            m = json.loads(data)
            m["schema_version"] = 999
            return name, json.dumps(m).encode()
        return name, data

    cases = [
        (damage, "damaged in the backup"),
        (escape, "unsafe path"),
        (newer, "newer FlightLens"),
    ]
    for i, (change, message) in enumerate(cases):
        bad = _rewrite(good, tmp_path / f"bad{i}.zip", change)
        target = tmp_path / f"r{i}"
        with pytest.raises(backup.BackupError, match=message):
            backup.restore(bad, target)
        assert not target.exists()  # nothing left behind
    assert not (tmp_path / "evil.txt").exists()
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(backup.BackupError, match="not a FlightLens backup"):
        backup.restore(junk, tmp_path / "rj")


def test_backup_and_restore_commands(library: Path, tmp_path: Path) -> None:
    out = tmp_path / "cli.zip"
    r = runner.invoke(cli.app, ["backup", str(out), "--data", str(library)])
    assert r.exit_code == 0 and "3 flights backed up" in r.output
    r = runner.invoke(cli.app, ["restore", str(out), "--data", str(tmp_path / "cli-restored")])
    assert r.exit_code == 0 and "Restored 3 flights" in r.output
    r = runner.invoke(cli.app, ["restore", str(out), "--data", str(tmp_path / "cli-restored")])
    assert r.exit_code == cli.EXIT_USAGE
    r = runner.invoke(cli.app, ["backup", str(out), "--data", str(tmp_path / "nowhere")])
    assert r.exit_code == cli.EXIT_USAGE


# --- retention -------------------------------------------------------------------------------


def test_prune_lists_first_and_keeps_reviewed(library: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    backup.restore(backup.create(library, tmp_path / "lib.zip").path, copy)
    store = Store(copy)
    future = time.time() + 10 * 365 * retention.DAY  # everything is old by then
    chosen = retention.candidates(store, 30, now=future)
    names = sorted(c.filename for c in chosen)
    assert names == ["plane-04-crash.bin", "quad-01-healthy.bin"]  # the reviewed one is kept
    assert len(retention.candidates(store, 30, keep_reviewed=False, now=future)) == 3
    assert retention.candidates(store, 365 * 50) == []  # nothing that old

    r = runner.invoke(cli.app, ["prune", "--older-than", "1", "--data", str(copy)])
    assert r.exit_code == 0 and "would be removed" in r.output
    assert len(store.list_flights()) == 3  # a dry run by default
    r = runner.invoke(cli.app, ["prune", "--older-than", "1", "--data", str(copy), "--yes"])
    assert r.exit_code == 0 and "Removed 2 flights" in r.output
    left = store.list_flights()
    assert [f["filename"] for f in left] == ["quad-10-vibration.bin"]
    assert not any((copy / "flights" / c.id).exists() for c in chosen)


def test_restore_refuses_crafted_paths_ids_and_sizes(
    library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = backup.create(library, tmp_path / "lib.zip").path

    def backslash(name: str, data: bytes) -> tuple[str, bytes]:
        # one harmless-looking POSIX name, but a path that escapes on Windows
        return (r"flights\..\..\evil.txt" if name.endswith("report.json") else name), data

    def manifest_id(name: str, data: bytes) -> tuple[str, bytes]:
        if name == "manifest.json":
            m = json.loads(data)
            m["flights"][0]["id"] = "../../outside"
            return name, json.dumps(m).encode()
        return name, data

    def database_id(name: str, data: bytes) -> tuple[str, bytes]:
        if name == "flightlens.db":
            db_file = tmp_path / "hostile.db"
            db_file.write_bytes(data)
            db = sqlite3.connect(db_file)
            db.execute("PRAGMA foreign_keys = OFF")
            db.execute("UPDATE flights SET id='../../../elsewhere' WHERE rowid=1")
            db.commit()
            db.close()
            return name, db_file.read_bytes()
        return name, data

    def trigger(name: str, data: bytes) -> tuple[str, bytes]:
        if name == "flightlens.db":
            db_file = tmp_path / "trigger.db"
            db_file.write_bytes(data)
            db = sqlite3.connect(db_file)
            db.execute(
                "CREATE TRIGGER t AFTER UPDATE ON jobs BEGIN UPDATE flights SET filename='x'; END"
            )
            db.commit()
            db.close()
            return name, db_file.read_bytes()
        return name, data

    cases = [
        (backslash, "unsafe path"),
        (manifest_id, "invalid flight entry"),
        (database_id, "invalid flight ids"),
        (trigger, "contains a trigger"),
    ]
    for i, (change, message) in enumerate(cases):
        bad = _rewrite(good, tmp_path / f"crafted{i}.zip", change)
        target = tmp_path / f"c{i}"
        with pytest.raises(backup.BackupError, match=message):
            backup.restore(bad, target)
        assert not target.exists()
    assert not (tmp_path / "evil.txt").exists()

    # a backup that would not fit on the disk is refused before anything is written
    monkeypatch.setattr(backup, "DISK_MARGIN", 1 << 62)
    with pytest.raises(backup.BackupError, match="GB is free"):
        backup.restore(good, tmp_path / "full")
    assert not (tmp_path / "full").exists()
