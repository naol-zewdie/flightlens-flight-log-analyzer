"""Local storage: original logs, analyzed signals, reports, and a SQLite index.

Layout under the data directory::

    flightlens.db                 index: flights, jobs, aircraft, profiles, reviews
                                  (schema versioned, see flightlens.schema)
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
from flightlens.provenance import stale_reasons
from flightlens.schema import migrate


def safe_name(signal: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in signal)


KNOWN_SUFFIXES = (".ulg", ".bin", ".tlog")


def safe_suffix(filename: str) -> str:
    """File extension to use on disk; anything unexpected becomes ``.log``."""
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in KNOWN_SUFFIXES else ".log"


def _note_out(row: Any) -> dict[str, Any]:
    n = dict(row)
    n["tags"] = json.loads(n["tags"]) if n.get("tags") else []
    n["view"] = json.loads(n["view"]) if n.get("view") else None
    return n


def _video_out(row: Any) -> dict[str, Any]:
    v = dict(row)
    v["sync_points"] = json.loads(v["sync_points"]) if v.get("sync_points") else []
    return v


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
        self.schema_from, self.schema_version = migrate(self._db, self.root / "flightlens.db")
        # Jobs left queued or processing are picked up by the job runner when the
        # server starts (``resume_jobs``); opening the library elsewhere (the CLI,
        # a backup) must not touch jobs a running server is working on.

    # --- health --------------------------------------------------------------

    def ping(self) -> bool:
        try:
            with self._lock:
                self._db.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def counts(self) -> dict[str, Any]:
        """Flights by status and by verdict, and the size of the original logs."""
        with self._lock:
            status = self._db.execute(
                "SELECT status, count(*) FROM flights GROUP BY status"
            ).fetchall()
            verdict = self._db.execute(
                "SELECT verdict, count(*) FROM flights WHERE verdict IS NOT NULL GROUP BY verdict"
            ).fetchall()
            size = self._db.execute("SELECT coalesce(sum(size_bytes), 0) FROM flights").fetchone()
        return {
            "status": {r[0]: r[1] for r in status},
            "verdict": {r[0]: r[1] for r in verdict},
            "original_bytes": int(size[0]),
        }

    # --- jobs after a restart ------------------------------------------------

    def resume_jobs(self, max_attempts: int) -> tuple[list[tuple[str, str]], int]:
        """Jobs a stopped server left unfinished: (jobs to run again, jobs given up).

        A job that was already started ``max_attempts`` times is failed instead:
        a log that stops the server every time it is analyzed must not stop it forever.
        """
        resume: list[tuple[str, str]] = []
        given_up = 0
        with self._lock:
            rows = self._db.execute(
                "SELECT j.id, j.flight_id, j.attempts FROM jobs j JOIN flights f "
                "ON f.id = j.flight_id WHERE j.status IN ('queued','processing') "
                "ORDER BY j.created_at"
            ).fetchall()
            for job_id, flight_id, attempts in rows:
                if attempts >= max_attempts:
                    error = (
                        f"Analysis was interrupted {attempts} times by a server stop; "
                        "re-analyze to try again"
                    )
                    status = "failed"
                    given_up += 1
                else:
                    error, status = None, "queued"
                    resume.append((job_id, flight_id))
                self._db.execute(
                    "UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?",
                    (status, error, time.time(), job_id),
                )
                self._db.execute(
                    "UPDATE flights SET status=?, error=? WHERE id=?", (status, error, flight_id)
                )
            # jobs whose flight was deleted
            self._db.execute(
                "UPDATE jobs SET status='failed', error='The flight was deleted' "
                "WHERE status IN ('queued','processing') "
                "AND flight_id NOT IN (SELECT id FROM flights)"
            )
            self._db.commit()
        return resume, given_up

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
                "INSERT INTO jobs (id, flight_id, status, error, created_at, updated_at) "
                "VALUES (?,?, 'queued', NULL, ?, ?)",
                (job_id, flight_id, now, now),
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
                "INSERT INTO jobs (id, flight_id, status, error, created_at, updated_at) "
                "VALUES (?,?, 'queued', NULL, ?, ?)",
                (job_id, flight_id, now, now),
            )
            self._db.commit()
        return job_id

    def set_job(self, job_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status=?, error=?, updated_at=?, "
                "attempts = attempts + (? = 'processing') WHERE id=?",
                (status, error, time.time(), status, job_id),
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

    # --- videos ---------------------------------------------------------------

    VIDEO_FIELDS = ("offset_s", "rate", "sync_method", "sync_points", "label", "duration_s")

    def videos_dir(self, flight_id: str) -> Path:
        return self.flight_dir(flight_id) / "videos"

    def add_video(self, flight_id: str, row: dict[str, Any]) -> dict[str, Any]:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self._lock:
            self._db.execute(
                f"INSERT INTO videos (flight_id, {cols}) VALUES (?, {marks})",
                (flight_id, *row.values()),
            )
            self._db.commit()
        video = self.get_video(str(row["id"]))
        assert video is not None
        return video

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        return _video_out(r) if r else None

    def list_videos(self, flight_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM videos WHERE flight_id=? ORDER BY uploaded_at", (flight_id,)
            ).fetchall()
        return [_video_out(r) for r in rows]

    def video_path(self, video: dict[str, Any]) -> Path:
        return self.videos_dir(str(video["flight_id"])) / str(video["file"])

    def update_video(self, video_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        bad = set(changes) - set(self.VIDEO_FIELDS)
        if bad:
            raise ValueError(f"cannot change {', '.join(sorted(bad))}")
        if changes:
            values = [
                json.dumps(v) if k == "sync_points" and v is not None else v
                for k, v in changes.items()
            ]
            sets = ", ".join(f"{k}=?" for k in changes)
            with self._lock:
                self._db.execute(f"UPDATE videos SET {sets} WHERE id=?", (*values, video_id))
                self._db.commit()
        return self.get_video(video_id)

    def delete_video(self, video_id: str) -> bool:
        video = self.get_video(video_id)
        if video is None:
            return False
        path = self.video_path(video)
        if path.exists():
            path.chmod(0o644)
            path.unlink()
        with self._lock:
            self._db.execute("DELETE FROM videos WHERE id=?", (video_id,))
            self._db.commit()
        return True

    def video_bytes(self) -> dict[str, int]:
        """Total size of each flight's videos."""
        with self._lock:
            rows = self._db.execute(
                "SELECT flight_id, coalesce(sum(size_bytes), 0) FROM videos GROUP BY flight_id"
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    # --- notes and debriefs ----------------------------------------------------

    NOTE_FIELDS = ("t_start", "t_end", "text", "tags", "view", "author")

    def add_note(self, flight_id: str, note: dict[str, Any]) -> dict[str, Any]:
        note_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO notes (id, flight_id, t_start, t_end, text, tags, view, author, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    note_id,
                    flight_id,
                    note["t_start"],
                    note.get("t_end"),
                    note["text"],
                    json.dumps(note.get("tags") or []),
                    json.dumps(note["view"]) if note.get("view") is not None else None,
                    note.get("author"),
                    now,
                    now,
                ),
            )
            self._db.commit()
        out = self.get_note(note_id)
        assert out is not None
        return out

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return _note_out(r) if r else None

    def list_notes(self, flight_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM notes WHERE flight_id=? ORDER BY t_start, created_at", (flight_id,)
            ).fetchall()
        return [_note_out(r) for r in rows]

    def update_note(self, note_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        bad = set(changes) - set(self.NOTE_FIELDS)
        if bad:
            raise ValueError(f"cannot change {', '.join(sorted(bad))}")
        values = [
            json.dumps(v) if k in ("tags", "view") and v is not None else v
            for k, v in changes.items()
        ]
        sets = ", ".join(f"{k}=?" for k in changes)
        with self._lock:
            self._db.execute(
                f"UPDATE notes SET {sets}{', ' if sets else ''}updated_at=? WHERE id=?",
                (*values, time.time(), note_id),
            )
            self._db.commit()
        return self.get_note(note_id)

    def delete_note(self, note_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM notes WHERE id=?", (note_id,))
            self._db.commit()
        return cur.rowcount > 0

    def note_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT flight_id, COUNT(*) FROM notes GROUP BY flight_id"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_debrief(self, flight_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM debriefs WHERE flight_id=?", (flight_id,)
            ).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["actions"] = json.loads(d["actions"]) if d.get("actions") else []
        return d

    def put_debrief(self, flight_id: str, debrief: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._db.execute(
                "INSERT INTO debriefs (flight_id, status, summary, conclusion, actions, author, "
                "updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(flight_id) DO UPDATE SET "
                "status=excluded.status, summary=excluded.summary, "
                "conclusion=excluded.conclusion, actions=excluded.actions, "
                "author=excluded.author, updated_at=excluded.updated_at",
                (
                    flight_id,
                    debrief["status"],
                    debrief.get("summary"),
                    debrief.get("conclusion"),
                    json.dumps(debrief.get("actions") or []),
                    debrief.get("author"),
                    time.time(),
                ),
            )
            self._db.commit()
        out = self.get_debrief(flight_id)
        assert out is not None
        return out

    def debrief_statuses(self) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT flight_id, status FROM debriefs").fetchall()
        return {r[0]: r[1] for r in rows}

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

    def read_tuning(self, flight_id: str) -> dict[str, Any] | None:
        path = self.flight_dir(flight_id) / "analysis" / "tuning.json"
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

    def read_replay_meta(self, flight_id: str) -> dict[str, Any] | None:
        path = self.flight_dir(flight_id) / "analysis" / "replay.json"
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def read_replay_table(self, flight_id: str) -> dict[str, np.ndarray] | None:
        from flightlens.replay import read_columns

        return read_columns(self.flight_dir(flight_id) / "analysis")

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
