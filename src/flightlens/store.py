"""Local storage: original logs, analyzed signals, reports, and a SQLite index.

Layout under the data directory::

    flightlens.db                 flights and jobs index
    flights/<id>/original.<ext>   the uploaded log, never modified
    flights/<id>/analysis/report.json         report, summary, events, signal catalogue
    flights/<id>/analysis/signals/*.parquet   one file per signal (t, v)

A flight id is the first 16 hex characters of the log's SHA-256, so the same
file uploaded twice maps to the same flight.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from flightlens.profiles import Profile
from flightlens.provenance import stale_reasons, versions_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS flights (
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
    aircraft      TEXT,
    serial        TEXT,
    metrics       TEXT              -- JSON, flightlens.metrics
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    flight_id   TEXT NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,        -- queued | processing | done | failed
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    name        TEXT PRIMARY KEY,
    source      TEXT NOT NULL,        -- user | learned | file
    overrides   TEXT NOT NULL,        -- JSON {check_id: {key: value}}
    notes       TEXT,
    basis       TEXT,                 -- JSON, learned profiles only
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
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
);
CREATE TABLE IF NOT EXISTS aircraft_profiles (
    aircraft_key  TEXT PRIMARY KEY,   -- fleet.aircraft_key: label:<name> or sn:<serial>
    profile_name  TEXT NOT NULL REFERENCES profiles(name) ON DELETE CASCADE ON UPDATE CASCADE
);
"""


def safe_name(signal: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in signal)


KNOWN_SUFFIXES = (".ulg", ".bin", ".tlog")


def safe_suffix(filename: str) -> str:
    """File extension to use on disk; anything unexpected becomes ``.log``."""
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in KNOWN_SUFFIXES else ".log"


def _remove_tree(path: Path) -> None:
    """Delete a folder, including the read-only original log inside it."""
    if not path.exists():
        return
    for p in path.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
    shutil.rmtree(path)


@dataclass
class Store:
    root: Path

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "flights").mkdir(exist_ok=True)
        (self.root / "uploads").mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "flightlens.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)
        self._migrate()
        # Jobs interrupted by a restart are failed, never left "processing".
        self._db.execute(
            "UPDATE jobs SET status='failed', error='Interrupted by a restart; upload again' "
            "WHERE status IN ('queued','processing')"
        )
        self._db.execute(
            "UPDATE flights SET status='failed', error='Interrupted by a restart; upload again' "
            "WHERE status IN ('queued','processing')"
        )
        self._db.commit()

    def _migrate(self) -> None:
        """Add columns that newer versions need to a database made by an older one."""
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(flights)")}
        for column in ("serial", "metrics", "versions"):
            if column not in have:
                self._db.execute(f"ALTER TABLE flights ADD COLUMN {column} TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS flights_serial ON flights(serial)")
        # Analyses made before versions were indexed: read them from the stored report.
        rows = self._db.execute(
            "SELECT id FROM flights WHERE status='done' AND versions IS NULL"
        ).fetchall()
        for row in rows:
            report = self.read_report(row["id"])
            if report is not None:
                self._db.execute(
                    "UPDATE flights SET versions=? WHERE id=?",
                    (json.dumps(versions_of(report)), row["id"]),
                )

    # --- paths ---------------------------------------------------------------

    def flight_dir(self, flight_id: str) -> Path:
        return self.root / "flights" / flight_id

    def original_path(self, flight_id: str) -> Path | None:
        found = sorted(self.flight_dir(flight_id).glob("original.*"))
        return found[0] if found else None

    # --- flights -------------------------------------------------------------

    def find_by_sha(self, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM flights WHERE sha256=?", (sha256,)).fetchone()
        return dict(row) if row else None

    def create_flight(
        self, upload: Path, sha256: str, filename: str, log_format: str
    ) -> tuple[str, str] | None:
        """Move an upload into place and queue it.

        Returns (flight_id, job_id), or None if a flight with this SHA-256
        already exists (also when two identical uploads race).
        """
        flight_id = sha256[:16]
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        # Claim the id in the database first, so only one upload moves a file.
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO flights (id, sha256, filename, log_format, size_bytes, "
                    "uploaded_at, status) VALUES (?,?,?,?,?,?, 'queued')",
                    (flight_id, sha256, filename, log_format, upload.stat().st_size, now),
                )
                self._db.commit()
            except sqlite3.IntegrityError:
                self._db.rollback()
                return None
        try:
            fdir = self.flight_dir(flight_id)
            _remove_tree(fdir)  # leftovers from an earlier, partly failed delete
            fdir.mkdir(parents=True)
            dest = fdir / f"original{safe_suffix(filename)}"
            shutil.move(str(upload), dest)
            dest.chmod(0o444)  # the original is evidence; keep it read-only
        except Exception:
            with self._lock:
                self._db.execute("DELETE FROM flights WHERE id=?", (flight_id,))
                self._db.commit()
            raise
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs VALUES (?,?, 'queued', NULL, ?, ?)", (job_id, flight_id, now, now)
            )
            self._db.commit()
        return flight_id, job_id

    def requeue(self, flight_id: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._db.execute(
                "UPDATE flights SET status='queued', error=NULL WHERE id=?", (flight_id,)
            )
            self._db.execute(
                "INSERT INTO jobs VALUES (?,?, 'queued', NULL, ?, ?)", (job_id, flight_id, now, now)
            )
            self._db.commit()
        return job_id

    def set_job(self, job_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                (status, error, time.time(), job_id),
            )
            row = self._db.execute("SELECT flight_id FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                self._db.execute(
                    "UPDATE flights SET status=?, error=? WHERE id=?",
                    (status, error, row["flight_id"]),
                )
            self._db.commit()

    def record_result(self, flight_id: str, summary: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE flights SET firmware=?, vehicle_type=?, hardware=?, serial=?, metrics=?, "
                "start_time=?, duration_s=?, verdict=?, score=?, n_problem=?, n_attention=?, "
                "versions=? WHERE id=?",
                (
                    summary.get("firmware"),
                    summary.get("vehicle_type"),
                    summary.get("hardware"),
                    summary.get("serial"),
                    json.dumps(summary["metrics"]) if summary.get("metrics") is not None else None,
                    summary.get("start_time"),
                    summary.get("duration_s"),
                    summary.get("verdict"),
                    summary.get("score"),
                    summary.get("n_problem"),
                    summary.get("n_attention"),
                    json.dumps(summary["versions"]) if summary.get("versions") else None,
                    flight_id,
                ),
            )
            # Same board as an aircraft the user already named: take that name.
            if summary.get("serial"):
                self._db.execute(
                    "UPDATE flights SET aircraft=(SELECT aircraft FROM flights f "
                    "WHERE f.serial=? AND f.aircraft IS NOT NULL AND f.id<>? "
                    "ORDER BY f.uploaded_at DESC LIMIT 1) WHERE id=? AND aircraft IS NULL",
                    (summary["serial"], flight_id, flight_id),
                )
            self._db.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_flight(self, flight_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
        return dict(row) if row else None

    def list_flights(
        self, verdict: str | None = None, q: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM flights WHERE 1=1"
        args: list[Any] = []
        if verdict:
            sql += " AND verdict=?"
            args.append(verdict)
        if q:
            sql += " AND (filename LIKE ? OR firmware LIKE ? OR hardware LIKE ? OR aircraft LIKE ?)"
            args += [f"%{q}%"] * 4
        sql += " ORDER BY uploaded_at DESC"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def set_aircraft(self, flight_id: str, label: str | None, same_board: bool = False) -> int:
        """Label one flight, or every flight from the same autopilot board; returns the count."""
        with self._lock:
            if same_board:
                cur = self._db.execute(
                    "UPDATE flights SET aircraft=? WHERE serial IS NOT NULL AND serial="
                    "(SELECT serial FROM flights WHERE id=?)",
                    (label, flight_id),
                )
                if cur.rowcount:
                    self._db.commit()
                    return cur.rowcount
            cur = self._db.execute("UPDATE flights SET aircraft=? WHERE id=?", (label, flight_id))
            self._db.commit()
            return cur.rowcount

    def stale_details(self) -> dict[str, list[str]]:
        """Analyzed flights whose analysis is out of date, with the reasons."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, metrics, versions FROM flights WHERE status='done'"
            ).fetchall()
        out = {}
        for r in rows:
            versions = json.loads(r["versions"]) if r["versions"] else None
            reasons = stale_reasons(versions, has_metrics=r["metrics"] is not None)
            if reasons:
                out[r["id"]] = reasons
        return out

    def stale_flights(self) -> list[str]:
        """Analyzed flights made by an older version of a check or mapping."""
        return list(self.stale_details())

    # --- engineer reviews --------------------------------------------------------

    def set_review(
        self, flight_id: str, finding: dict[str, Any], status: str, note: str | None
    ) -> None:
        from flightlens.reviews import finding_key

        with self._lock:
            self._db.execute(
                "INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(flight_id, finding_key) DO UPDATE SET status=excluded.status, "
                "note=excluded.note, reviewed_at=excluded.reviewed_at, "
                "severity=excluded.severity, title=excluded.title, "
                "t_start=excluded.t_start, t_end=excluded.t_end",
                (
                    flight_id,
                    finding_key(finding),
                    finding["check_id"],
                    finding["severity"],
                    finding["title"],
                    finding.get("start"),
                    finding.get("end"),
                    status,
                    note,
                    time.time(),
                ),
            )
            self._db.commit()

    def clear_review(self, flight_id: str, key: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM reviews WHERE flight_id=? AND finding_key=?", (flight_id, key)
            )
            self._db.commit()
        return cur.rowcount > 0

    def reviews_for(self, flight_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM reviews WHERE flight_id=? ORDER BY t_start", (flight_id,)
            ).fetchall()
        return {r["finding_key"]: dict(r) for r in rows}

    def all_reviews(self) -> list[dict[str, Any]]:
        """Every review, with the flight's file name and stored original's name."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.*, f.filename FROM reviews r JOIN flights f ON f.id = r.flight_id "
                "ORDER BY f.uploaded_at, r.t_start"
            ).fetchall()
        out = []
        for r in rows:
            original = self.original_path(r["flight_id"])
            if original is not None:
                out.append({**dict(r), "original": original.name})
        return out

    def review_counts(self) -> dict[str, int]:
        """Number of reviewed findings per flight."""
        with self._lock:
            rows = self._db.execute(
                "SELECT flight_id, COUNT(*) AS n FROM reviews GROUP BY flight_id"
            ).fetchall()
        return {r["flight_id"]: r["n"] for r in rows}

    def delete_flight(self, flight_id: str) -> bool:
        """Delete the files first, then the index row.

        If the files cannot be removed (for example a file is open on Windows),
        this raises and the flight stays listed, so the user can retry.
        """
        _remove_tree(self.flight_dir(flight_id))
        with self._lock:
            cur = self._db.execute("DELETE FROM flights WHERE id=?", (flight_id,))
            self._db.commit()
        return cur.rowcount > 0

    # --- threshold profiles ----------------------------------------------------

    def list_profiles(self) -> list[Profile]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM profiles ORDER BY name").fetchall()
        return [_profile(r) for r in rows]

    def get_profile(self, name: str) -> Profile | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM profiles WHERE name=?", (name,)).fetchone()
        return _profile(row) if row else None

    def save_profile(self, profile: Profile) -> None:
        """Create or replace a profile (by name); assignments are kept."""
        with self._lock:
            self._db.execute(
                "INSERT INTO profiles (name, source, overrides, notes, basis, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET source=excluded.source, "
                "overrides=excluded.overrides, notes=excluded.notes, basis=excluded.basis, "
                "created_at=excluded.created_at",
                (
                    profile.name,
                    profile.source,
                    json.dumps(profile.overrides, sort_keys=True),
                    profile.notes,
                    json.dumps(profile.basis) if profile.basis else None,
                    time.time(),
                ),
            )
            self._db.commit()

    def delete_profile(self, name: str) -> bool:
        """Delete a profile; aircraft that used it go back to the defaults."""
        with self._lock:
            cur = self._db.execute("DELETE FROM profiles WHERE name=?", (name,))
            self._db.commit()
        return cur.rowcount > 0

    def assign_profile(self, aircraft_key: str, name: str | None) -> None:
        with self._lock:
            if name is None:
                self._db.execute(
                    "DELETE FROM aircraft_profiles WHERE aircraft_key=?", (aircraft_key,)
                )
            else:
                self._db.execute(
                    "INSERT INTO aircraft_profiles VALUES (?,?) ON CONFLICT(aircraft_key) "
                    "DO UPDATE SET profile_name=excluded.profile_name",
                    (aircraft_key, name),
                )
            self._db.commit()

    def profile_assignments(self) -> dict[str, Profile]:
        """Aircraft key to its assigned profile."""
        with self._lock:
            rows = self._db.execute(
                "SELECT a.aircraft_key, p.* FROM aircraft_profiles a "
                "JOIN profiles p ON p.name = a.profile_name"
            ).fetchall()
        return {r["aircraft_key"]: _profile(r) for r in rows}

    def labels_by_serial(self) -> dict[str, str]:
        """Board serial to the name the user gave that aircraft (latest wins)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT serial, aircraft FROM flights WHERE serial IS NOT NULL "
                "AND aircraft IS NOT NULL ORDER BY uploaded_at"
            ).fetchall()
        return {r["serial"]: r["aircraft"] for r in rows}

    # --- analysis results ------------------------------------------------------

    def read_report(self, flight_id: str) -> dict[str, Any] | None:
        path = self.flight_dir(flight_id) / "analysis" / "report.json"
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def read_spectrum(self, flight_id: str) -> dict[str, Any] | None:
        path = self.flight_dir(flight_id) / "analysis" / "spectrum.json"
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def read_signal(self, flight_id: str, name: str) -> tuple[np.ndarray, np.ndarray] | None:
        path = self.flight_dir(flight_id) / "analysis" / "signals" / f"{safe_name(name)}.parquet"
        if not path.exists():
            return None
        table = pq.read_table(path)
        t = table.column("t").to_numpy()
        v = table.column("v").to_numpy(zero_copy_only=False).astype(np.float64)
        return t, v


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(
        name=row["name"],
        overrides=json.loads(row["overrides"]),
        source=row["source"],
        notes=row["notes"] or "",
        basis=json.loads(row["basis"]) if row["basis"] else None,
    )


def write_signals(directory: Path, signals: dict[str, Any]) -> None:
    """Write each signal to its own Parquet file (called by the worker process)."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, sig in signals.items():
        table = pa.table({"t": sig.t, "v": sig.values})
        pq.write_table(table, directory / f"{safe_name(name)}.parquet", compression="zstd")
