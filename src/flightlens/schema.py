"""The library database's schema and its versioned migrations.

The schema version lives in SQLite's ``PRAGMA user_version``. Opening a library
applies every migration it has not had yet, in order, each in its own
transaction that also records the new version, so an interrupted upgrade
leaves the database at the last complete step, never half-way through one.

Before an existing library is upgraded, a copy of its database is kept next to
it (``flightlens.db.before-v<N>``). A database made by a newer FlightLens than
this one is refused rather than misread.

Databases from before versioning (``user_version`` 0, made by 0.1 and early
0.2 builds) already have some of the tables and columns. Every migration is
therefore idempotent: it creates only what is missing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from flightlens.provenance import versions_of


class SchemaError(RuntimeError):
    """The database cannot be used by this version of FlightLens."""


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _add_column(db: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _v1_flights_and_jobs(db: sqlite3.Connection, data: Path) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS flights (
            id            TEXT PRIMARY KEY,
            sha256        TEXT UNIQUE NOT NULL,
            filename      TEXT NOT NULL,
            log_format    TEXT,
            size_bytes    INTEGER,
            uploaded_at   REAL NOT NULL,
            status        TEXT NOT NULL,      -- queued | processing | done | failed
            error         TEXT,
            firmware      TEXT,
            vehicle_type  TEXT,
            hardware      TEXT,
            start_time    TEXT,
            duration_s    REAL,
            verdict       TEXT,
            score         INTEGER,
            n_problem     INTEGER,
            n_attention   INTEGER,
            aircraft      TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            flight_id   TEXT NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
            status      TEXT NOT NULL,        -- queued | processing | done | failed
            error       TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        )"""
    )


def _v2_aircraft_identity_and_metrics(db: sqlite3.Connection, data: Path) -> None:
    _add_column(db, "flights", "serial", "TEXT")
    _add_column(db, "flights", "metrics", "TEXT")  # JSON, flightlens.metrics
    db.execute("CREATE INDEX IF NOT EXISTS flights_serial ON flights(serial)")


def _v3_threshold_profiles(db: sqlite3.Connection, data: Path) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS profiles (
            name        TEXT PRIMARY KEY,
            source      TEXT NOT NULL,        -- user | learned | file
            overrides   TEXT NOT NULL,        -- JSON {check_id: {key: value}}
            notes       TEXT,
            basis       TEXT,                 -- JSON, learned profiles only
            created_at  REAL NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS aircraft_profiles (
            aircraft_key  TEXT PRIMARY KEY,   -- fleet.aircraft_key
            profile_name  TEXT NOT NULL
                REFERENCES profiles(name) ON DELETE CASCADE ON UPDATE CASCADE
        )"""
    )


def _v4_reviews_and_analysis_versions(db: sqlite3.Connection, data: Path) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS reviews (
            flight_id    TEXT NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
            finding_key  TEXT NOT NULL,       -- reviews.finding_key: check@start second
            check_id     TEXT NOT NULL,
            severity     TEXT NOT NULL,
            title        TEXT NOT NULL,
            t_start      REAL,
            t_end        REAL,
            status       TEXT NOT NULL,       -- confirmed | false_alarm | accepted
            note         TEXT,
            reviewed_at  REAL NOT NULL,
            PRIMARY KEY (flight_id, finding_key)
        )"""
    )
    _add_column(db, "flights", "versions", "TEXT")  # JSON, provenance.versions_of
    # analyses made before versions were indexed: read them from the stored report
    rows = db.execute("SELECT id FROM flights WHERE status='done' AND versions IS NULL")
    for (flight_id,) in rows.fetchall():
        report = data / "flights" / flight_id / "analysis" / "report.json"
        if report.exists():
            versions = versions_of(json.loads(report.read_text(encoding="utf-8")))
            db.execute(
                "UPDATE flights SET versions=? WHERE id=?", (json.dumps(versions), flight_id)
            )


def _v5_job_attempts(db: sqlite3.Connection, data: Path) -> None:
    # how often a job was started: a job that keeps stopping the server is given up
    _add_column(db, "jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")


Migration = Callable[[sqlite3.Connection, Path], None]
MIGRATIONS: tuple[tuple[int, str, Migration], ...] = (
    (1, "flights and analysis jobs", _v1_flights_and_jobs),
    (2, "aircraft identity and trend metrics", _v2_aircraft_identity_and_metrics),
    (3, "threshold profiles", _v3_threshold_profiles),
    (4, "engineer reviews and analysis versions", _v4_reviews_and_analysis_versions),
    (5, "job attempts, so interrupted jobs resume", _v5_job_attempts),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def version(db: sqlite3.Connection) -> int:
    return int(db.execute("PRAGMA user_version").fetchone()[0])


def _is_empty(db: sqlite3.Connection) -> bool:
    tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    return bool(tables == 0)


def migrate(db: sqlite3.Connection, db_path: Path) -> tuple[int, int]:
    """Bring the database to ``SCHEMA_VERSION``; returns (version before, after)."""
    before = version(db)
    if before > SCHEMA_VERSION:
        raise SchemaError(
            f"{db_path} was written by a newer FlightLens (database schema {before}; this "
            f"version knows up to {SCHEMA_VERSION}). Upgrade FlightLens to open it."
        )
    if before == SCHEMA_VERSION:
        return before, before
    if not _is_empty(db):
        # keep the database as it was before the upgrade, in case it goes wrong
        # An existing copy is from an earlier, failed attempt: it holds the older
        # database, so it is kept, never overwritten with a half-upgraded one.
        copy = db_path.with_name(f"{db_path.name}.before-v{SCHEMA_VERSION}")
        if not copy.exists():
            dest = sqlite3.connect(copy)
            try:
                db.backup(dest)
            finally:
                dest.close()
    data = db_path.parent
    db.commit()
    implicit = db.isolation_level
    db.isolation_level = None  # explicit transactions: one per migration step
    try:
        for number, _what, step in MIGRATIONS:
            if number <= before:
                continue
            db.execute("BEGIN")
            try:
                step(db, data)
                db.execute(f"PRAGMA user_version = {number}")
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
    finally:
        db.isolation_level = implicit
    return before, SCHEMA_VERSION
