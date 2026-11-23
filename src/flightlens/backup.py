"""Backing up and restoring a FlightLens library.

A backup is one ``.zip``:

- ``flightlens.db``: a consistent snapshot of the database, taken with SQLite's
  online backup, so it is safe to run while ``flightlens serve`` is working;
- ``flights/<id>/original.*``: every original log, unchanged;
- ``flights/<id>/analysis/...``: the analysis results (left out with
  ``include_analysis=False``; they are then rebuilt on restore);
- ``manifest.json``: FlightLens and schema version, and each original's SHA-256.

Restore only writes into an empty or new folder, never over a library. It
checks every original against its SHA-256 before anything is put in place,
refuses archive entries that would land outside the folder, and refuses a
backup made by a newer FlightLens.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from flightlens import __version__
from flightlens.schema import SCHEMA_VERSION

FORMAT = "flightlens-backup"
FORMAT_VERSION = 1
DB_NAME = "flightlens.db"


FLIGHT_ID = re.compile(r"^[0-9a-f]{16}$")
"""A flight id is the first 16 hex digits of the log's SHA-256 (``Store.create_flight``)."""
ORIGINAL = re.compile(r"^original\.[a-z0-9]{1,8}$")
DISK_MARGIN = 256 * 1024**2


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupSummary:
    path: Path
    flights: int
    originals_bytes: int
    included_analysis: bool
    size_bytes: int


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def create(data_dir: Path, out: Path, include_analysis: bool = True) -> BackupSummary:
    """Write a backup of the library in ``data_dir`` to ``out`` (a .zip)."""
    db_path = data_dir / DB_NAME
    if not db_path.is_file():
        raise BackupError(f"{data_dir} is not a FlightLens library (no {DB_NAME})")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / DB_NAME
        src = sqlite3.connect(db_path)
        dest = sqlite3.connect(snapshot)
        try:
            src.backup(dest)  # consistent even while the server writes
            schema = int(dest.execute("PRAGMA user_version").fetchone()[0])
            rows = dest.execute("SELECT id, sha256, filename FROM flights ORDER BY id").fetchall()
        finally:
            dest.close()
            src.close()

        flights: list[dict[str, Any]] = []
        total = 0
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.write(snapshot, DB_NAME)
            for flight_id, sha, filename in rows:
                fdir = data_dir / "flights" / flight_id
                originals = sorted(fdir.glob("original.*"))
                if not originals:
                    continue  # nothing to back up; the restored row will say the file is missing
                original = originals[0]
                total += original.stat().st_size
                z.write(original, f"flights/{flight_id}/{original.name}")
                if include_analysis and (fdir / "analysis").is_dir():
                    for f in sorted((fdir / "analysis").rglob("*")):
                        if f.is_file():
                            z.write(f, f"flights/{flight_id}/{f.relative_to(fdir).as_posix()}")
                flights.append(
                    {
                        "id": flight_id,
                        "sha256": sha,
                        "original": original.name,
                        "filename": filename,
                    }
                )
            manifest = {
                "format": FORMAT,
                "format_version": FORMAT_VERSION,
                "flightlens_version": __version__,
                "schema_version": schema,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "include_analysis": include_analysis,
                "flights": flights,
            }
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
    part.replace(out)
    return BackupSummary(out, len(flights), total, include_analysis, out.stat().st_size)


def _safe_member(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    # A backslash is a separator on Windows: "a\..\b" would escape there.
    if "\\" in name or p.is_absolute() or ".." in p.parts or any(":" in part for part in p.parts):
        raise BackupError(f"unsafe path in backup: {name!r}")
    return p


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as z:
            manifest: dict[str, Any] = json.loads(z.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise BackupError(f"{path.name} is not a FlightLens backup ({exc})") from None
    if manifest.get("format") != FORMAT:
        raise BackupError(f"{path.name} is not a FlightLens backup")
    for f in manifest.get("flights", []):
        if not (
            FLIGHT_ID.match(str(f.get("id", ""))) and ORIGINAL.match(str(f.get("original", "")))
        ):
            raise BackupError(
                f"{path.name} has an invalid flight entry; it was not made by FlightLens"
            )
    if (
        int(manifest.get("format_version", 0)) > FORMAT_VERSION
        or int(manifest.get("schema_version", 0)) > SCHEMA_VERSION
    ):
        raise BackupError(
            f"{path.name} was made by a newer FlightLens ({manifest.get('flightlens_version')}); "
            "upgrade FlightLens to restore it"
        )
    return manifest


def restore(
    backup: Path,
    data_dir: Path,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore ``backup`` into ``data_dir``, which must be empty or not exist yet."""
    manifest = read_manifest(backup)
    if data_dir.exists() and any(data_dir.iterdir()):
        raise BackupError(
            f"{data_dir} is not empty; restore into a new folder (a library is never overwritten)"
        )
    created = not data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=data_dir))
    ok = False
    try:
        with zipfile.ZipFile(backup) as z:
            needed = sum(i.file_size for i in z.infolist())
            free = shutil.disk_usage(data_dir).free
            if needed + DISK_MARGIN > free:
                raise BackupError(
                    f"restoring needs {needed / 1e9:.1f} GB but only {free / 1e9:.1f} GB is free"
                )
            for info in z.infolist():
                rel = _safe_member(info.filename)
                if info.is_dir() or rel.name == "manifest.json":
                    continue
                target = staging.joinpath(*rel.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not (staging / DB_NAME).is_file():
            raise BackupError("the backup has no database")
        _check_database(staging / DB_NAME)
        for f in manifest["flights"]:
            original = staging / "flights" / f["id"] / f["original"]
            if not original.is_file():
                raise BackupError(f"the backup is missing the original of {f['filename']}")
            if _sha256(original) != f["sha256"]:
                raise BackupError(f"{f['filename']} is damaged in the backup (SHA-256 differs)")
        # all checked: move into place
        for item in staging.iterdir():
            item.replace(data_dir / item.name)
        ok = True
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if not ok and created:
            shutil.rmtree(data_dir, ignore_errors=True)  # leave nothing behind
    for f in manifest["flights"]:
        (data_dir / "flights" / f["id"] / f["original"]).chmod(0o444)

    rebuilt = 0
    if not manifest.get("include_analysis", True):
        rebuilt = _reanalyze(data_dir, manifest, progress)
    from flightlens.store import Store

    Store(data_dir)  # opens and, for an older backup, upgrades the database
    return {"flights": len(manifest["flights"]), "reanalyzed": rebuilt, "manifest": manifest}


def _check_database(path: Path) -> None:
    """Refuse a database whose flight ids could point outside the library."""
    db = sqlite3.connect(path)
    try:
        # FlightLens creates no triggers or views; one in a backup would run on our writes
        extra = db.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')"
        ).fetchone()
        if extra is not None:
            raise BackupError(f"the backup's database contains a {extra[0]} ({extra[1]!r})")
        ids = [r[0] for r in db.execute("SELECT id FROM flights")]
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"the backup's database is unreadable ({exc})") from None
    finally:
        db.close()
    bad = [i for i in ids if not FLIGHT_ID.match(str(i))]
    if bad:
        raise BackupError(f"the backup's database has invalid flight ids, e.g. {bad[0]!r}")


def _reanalyze(
    data_dir: Path, manifest: dict[str, Any], progress: Callable[[str], None] | None
) -> int:
    """Rebuild analysis results that the backup left out."""
    from flightlens.store import Store
    from flightlens.worker import process_log

    store = Store(data_dir)
    n = 0
    for f in manifest["flights"]:
        row = store.get_flight(f["id"])
        if row is None or row["status"] != "done":
            continue
        if progress:
            progress(f["filename"])
        original = store.original_path(f["id"])
        assert original is not None
        try:
            summary = process_log(str(original), str(store.flight_dir(f["id"])))
        except Exception as exc:  # a log that no longer analyses is marked, not fatal
            job = store.requeue(f["id"])
            store.set_job(job, "failed", f"Analysis error after restore: {exc}")
            continue
        store.record_result(f["id"], summary)
        n += 1
    return n
