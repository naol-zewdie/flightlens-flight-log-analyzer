"""HTTP API and static web UI.

All endpoints are under ``/api``. The built web UI (``web/dist``) is served at
``/`` when present. Uploads are streamed to disk, hashed, format-checked and
queued; parsing happens in an isolated child process (see ``worker.py``).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from flightlens import __version__, fleet, media, parsers
from flightlens.checks import all_checks
from flightlens.debrief import render_debrief
from flightlens.htmlreport import render_html
from flightlens.lttb import lttb, with_gaps
from flightlens.metrics import METRICS
from flightlens.ops import Labels, Metrics, new_metrics, request_id
from flightlens.profiles import NAME_MAX, ProfileError, learn_profile, parse_profile
from flightlens.provenance import stale_reasons
from flightlens.reviews import NOTE_MAX, REVIEWABLE, STATUSES, finding_key, manifest_yaml, orphaned
from flightlens.reviews import with_keys as findings_with_reviews
from flightlens.store import Store, safe_suffix
from flightlens.worker import (
    DEFAULT_MAX_QUEUE,
    DEFAULT_TIMEOUT_S,
    DEFAULT_WORKERS,
    JobRunner,
)

MAX_UPLOAD_BYTES = 1024**3  # 1 GB, from the spec
MAX_VIDEO_BYTES = 4 * 1024**3
"""Default limit for one video; ``create_app(max_video_bytes=...)`` changes it."""
UPLOAD_ROUTES = (
    (re.compile(r"^/api/logs$"), "log"),
    (re.compile(r"^/api/flights/[^/]+/videos$"), "video"),
)
"""POST routes that receive a file: their size is checked before the body is read."""
MULTIPART_OVERHEAD = 1 << 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
CHUNK = 1 << 20
MAX_POINTS = 20_000
TRACK_POINTS = 3_000
MIN_FREE_BYTES = 512 * 1024**2
"""Uploads are refused when the data disk has less free space than this."""
SHUTDOWN_GRACE_S = 30.0
QUIET_ROUTES = ("/api/health", "/metrics", "/api/jobs/{job_id}")
"""Logged at DEBUG: probes, and the UI polling a job (static assets too)."""
SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
)

log = logging.getLogger("flightlens.server")
http_log = logging.getLogger("flightlens.http")


class FlightPatch(BaseModel):
    aircraft: str | None = None
    same_board: bool = False
    """Apply the label to every flight from the same autopilot board."""


class ProfileBody(BaseModel):
    description: str = ""
    checks: dict[str, dict[str, float]]


class ReviewBody(BaseModel):
    status: str
    """confirmed | false_alarm | accepted"""
    note: str = ""


TEXT_MAX = 5000
TAG_MAX = 32


class NoteBody(BaseModel):
    t_start: float = Field(ge=0)
    t_end: float | None = Field(None, ge=0)
    text: str = Field(min_length=1, max_length=NOTE_MAX)
    tags: list[Annotated[str, Field(min_length=1, max_length=TAG_MAX)]] = Field(
        default_factory=list, max_length=12
    )
    view: dict[str, Any] | None = None
    """Camera mode and the like, restored when the note is opened."""
    author: str | None = Field(None, max_length=80)


class NotePatch(BaseModel):
    t_start: float | None = Field(None, ge=0)
    t_end: float | None = Field(None, ge=0)
    text: str | None = Field(None, min_length=1, max_length=NOTE_MAX)
    tags: list[Annotated[str, Field(min_length=1, max_length=TAG_MAX)]] | None = Field(
        None, max_length=12
    )
    view: dict[str, Any] | None = None
    author: str | None = Field(None, max_length=80)


class DebriefBody(BaseModel):
    status: Literal["open", "closed"]
    summary: str = Field("", max_length=TEXT_MAX)
    conclusion: str = Field("", max_length=TEXT_MAX)
    actions: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=30
    )
    author: str | None = Field(None, max_length=80)


class VideoPatch(BaseModel):
    offset_s: float | None = None
    """log time = offset_s + rate * video time; null: not synced"""
    rate: float = Field(1.0, gt=0.9, lt=1.1)
    """Clock-rate correction from two or more sync points (1.0 = none)."""
    sync_method: Literal["none", "metadata", "manual", "points"] = "none"
    sync_points: list[tuple[float, float]] = Field(default_factory=list, max_length=50)
    label: str | None = Field(None, max_length=200)
    duration_s: float | None = Field(None, gt=0)
    """Reported by the browser when the server could not read it."""


class AssignBody(BaseModel):
    profile: str | None
    """Profile name, or null for the checks' defaults."""


class LearnBody(BaseModel):
    name: str | None = None


def create_app(
    data_dir: Path,
    web_dir: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
    read_only: bool = False,
    workers: int = DEFAULT_WORKERS,
    max_queue: int = DEFAULT_MAX_QUEUE,
    shutdown_grace_s: float = SHUTDOWN_GRACE_S,
    min_free_bytes: int = MIN_FREE_BYTES,
    max_video_bytes: int = MAX_VIDEO_BYTES,
) -> FastAPI:
    """Build the app.

    ``allowed_hosts`` lists the host names the server answers to (``"*"`` for
    any). Anything else is refused, which blocks DNS-rebinding attacks from web
    pages open in the user's browser. ``read_only`` refuses every change
    (uploads, edits, deletes, re-analysis), for a public demo.

    ``workers`` logs are analyzed at once; at most ``max_queue`` may wait. On
    shutdown, running analyses get ``shutdown_grace_s`` seconds to finish;
    anything unfinished is resumed at the next start.
    """
    store = Store(data_dir)
    if store.schema_from != store.schema_version:
        log.info(
            "library upgraded",
            extra={"schema_from": store.schema_from, "schema_to": store.schema_version},
        )
    metrics = new_metrics()
    runner = JobRunner(
        store, timeout_s=timeout_s, workers=workers, max_queue=max_queue, metrics=metrics
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        resumed = runner.start()
        log.info(
            "server started",
            extra={
                "version": __version__,
                "data": str(store.root.resolve()),
                "workers": workers,
                "resumed_jobs": resumed,
                "read_only": read_only,
            },
        )
        yield
        log.info("shutting down", extra={"running_jobs": runner.running()})
        result = await asyncio.to_thread(runner.stop, shutdown_grace_s)
        log.info("server stopped", extra=result)

    app = FastAPI(
        title="FlightLens",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.runner = runner
    app.state.metrics = metrics

    def upload_refusal(length: int, kind: str = "log") -> tuple[int, str, dict[str, str]] | None:
        """Why an upload of ``length`` bytes cannot be taken now (checked before the
        body is read, so a refused upload never reaches the disk)."""
        refusal = None
        if kind == "log" and runner.full():
            refusal = (
                503,
                f"The analysis queue is full ({runner.pending()} logs waiting); try again shortly",
                {"Retry-After": "30"},
            )
        elif disk_free() < min_free_bytes + length:
            refusal = (507, "The server is low on disk space; uploads are paused", {})
        if refusal is not None:
            metrics.inc("flightlens_uploads_total", {"result": f"refused_{refusal[0]}"})
        return refusal

    app.add_middleware(
        RequestGuard,
        allowed_hosts=tuple(h.lower() for h in allowed_hosts),
        read_only=read_only,
        upload_refusal=upload_refusal,
        limits={"log": MAX_UPLOAD_BYTES, "video": max_video_bytes},
    )
    app.add_middleware(ObservedRequests, metrics=metrics)  # outermost: sees refusals too

    def disk_free() -> int:
        return shutil.disk_usage(store.root).free

    def row_stale(row: dict[str, Any]) -> list[str]:
        """Why this flight's analysis is out of date (empty when current or not analyzed)."""
        if row["status"] != "done" and row["verdict"] is None:
            return []
        versions = json.loads(row["versions"]) if row.get("versions") else None
        return stale_reasons(versions, has_metrics=row.get("metrics") is not None)

    def flight_or_404(flight_id: str) -> dict[str, Any]:
        row = store.get_flight(flight_id)
        if row is None:
            raise HTTPException(404, "No flight with this id")
        return row

    def report_or_409(flight_id: str) -> dict[str, Any]:
        row = flight_or_404(flight_id)
        report = store.read_report(flight_id)
        if report is None:
            raise HTTPException(409, f"This flight is not analyzed yet (status: {row['status']})")
        return report

    # --- health & upload ---------------------------------------------------------

    @app.get("/api/health")
    def health(response: Response) -> dict[str, Any]:
        """Liveness and readiness: 503 when the database or the job workers are down."""
        free = disk_free()
        checks = {
            "database": store.ping(),
            "jobs": runner.alive(),
            "disk": free >= min_free_bytes,
        }
        if not (checks["database"] and checks["jobs"]):
            status = "down"
            response.status_code = 503
        else:
            status = "ok" if checks["disk"] else "degraded"  # reads work; uploads are refused
        return {
            "status": status,
            "version": __version__,
            "read_only": read_only,
            "checks": checks,
            "queued_jobs": runner.pending(),
            "running_jobs": runner.running(),
            "workers": workers,
            "disk_free_bytes": free,
            "uptime_s": round(time.time() - metrics.started, 1),
        }

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint() -> Response:
        """Prometheus metrics."""
        counts = store.counts()

        def series(values: dict[str, int], label: str) -> list[tuple[Labels, float]]:
            return [(((label, k),), v) for k, v in sorted(values.items())]

        gauges: list[tuple[str, str, list[tuple[Labels, float]]]] = [
            ("flightlens_info", "FlightLens version.", [((("version", __version__),), 1)]),
            ("flightlens_start_time_seconds", "When the server started.", [((), metrics.started)]),
            ("flightlens_schema_version", "Library database schema.", [((), store.schema_version)]),
            ("flightlens_flights", "Flights by status.", series(counts["status"], "status")),
            (
                "flightlens_flights_by_verdict",
                "Analyzed flights by verdict.",
                series(counts["verdict"], "verdict"),
            ),
            (
                "flightlens_original_bytes",
                "Size of all original logs.",
                [((), counts["original_bytes"])],
            ),
            ("flightlens_jobs_queued", "Jobs waiting to run.", [((), runner.pending())]),
            ("flightlens_jobs_running", "Jobs running now.", [((), runner.running())]),
            ("flightlens_workers", "Jobs that can run at once.", [((), workers)]),
            ("flightlens_disk_free_bytes", "Free space on the data disk.", [((), disk_free())]),
        ]
        return Response(
            metrics.render(gauges), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    # A plain ``def``: FastAPI runs it in a worker thread, so hashing and
    # copying a large file does not block other requests.
    @app.post("/api/logs", status_code=202)
    def upload(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        try:
            result = receive_upload(file)
        except HTTPException as exc:
            metrics.inc("flightlens_uploads_total", {"result": f"refused_{exc.status_code}"})
            raise
        metrics.inc(
            "flightlens_uploads_total", {"result": "duplicate" if result["duplicate"] else "queued"}
        )
        return result

    def receive_upload(file: UploadFile) -> dict[str, Any]:
        if runner.full():
            raise HTTPException(
                503,
                f"The analysis queue is full ({runner.pending()} logs waiting); try again shortly",
                headers={"Retry-After": "30"},
            )
        if disk_free() < min_free_bytes:
            raise HTTPException(507, "The server is low on disk space; uploads are paused")
        filename = Path(file.filename or "upload").name[:200]
        tmp = store.root / "uploads" / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as fh:
                while chunk := file.file.read(CHUNK):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "The file is larger than the 1 GB limit")
                    digest.update(chunk)
                    fh.write(chunk)
            if size == 0:
                raise HTTPException(400, "The file is empty")
            named = _with_name(tmp, filename)
            try:
                fmt = parsers.detect_format(named)
            except parsers.UnsupportedFormatError as exc:
                raise HTTPException(415, str(exc)) from None

            sha = digest.hexdigest()
            created = store.create_flight(named, sha, filename, fmt.value)
            if created is None:
                existing = store.find_by_sha(sha) or {"id": sha[:16], "status": "queued"}
                return {
                    "flight_id": existing["id"],
                    "job_id": None,
                    "duplicate": True,
                    "status": existing["status"],
                }
            flight_id, job_id = created
            runner.submit(job_id, flight_id)
            return {
                "flight_id": flight_id,
                "job_id": job_id,
                "duplicate": False,
                "status": "queued",
            }
        finally:
            for p in tmp.parent.glob(f"{tmp.stem}.*"):
                p.unlink(missing_ok=True)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        row = store.get_job(job_id)
        if row is None:
            raise HTTPException(404, "No job with this id")
        return row

    # --- flights -----------------------------------------------------------------

    @app.get("/api/flights")
    def flights(
        verdict: str | None = None,
        q: str | None = None,
        debrief: Literal["open", "closed", "none", "any"] | None = None,
    ) -> dict[str, Any]:
        """Flights, newest first; ``debrief`` filters by debrief status (``any``: has one)."""
        reviewed = store.review_counts()
        notes = store.note_counts()
        debriefs = store.debrief_statuses()
        rows = [
            {
                **row,
                "stale": bool(row_stale(row)),
                "reviewed": reviewed.get(row["id"], 0),
                "notes": notes.get(row["id"], 0),
                "debrief": debriefs.get(row["id"]),
            }
            for row in store.list_flights(verdict=verdict, q=q)
        ]
        if debrief == "any":
            rows = [r for r in rows if r["debrief"]]
        elif debrief == "none":
            rows = [r for r in rows if not r["debrief"]]
        elif debrief:
            rows = [r for r in rows if r["debrief"] == debrief]
        return {"flights": rows}

    @app.get("/api/flights/{flight_id}")
    def flight(flight_id: str) -> dict[str, Any]:
        row = flight_or_404(flight_id)
        report = store.read_report(flight_id)
        if report is None:
            return {"flight": row, "report": None}
        slim = {k: v for k, v in report.items() if k not in ("findings", "events")}
        return {"flight": row, "report": slim, "stale": row_stale(row)}

    @app.patch("/api/flights/{flight_id}")
    def patch_flight(flight_id: str, body: FlightPatch) -> dict[str, Any]:
        flight_or_404(flight_id)
        label = (body.aircraft or "").strip()[:80] or None
        updated = store.set_aircraft(flight_id, label, same_board=body.same_board)
        return {"flight": store.get_flight(flight_id), "updated": updated}

    # --- aircraft ------------------------------------------------------------------

    def aircraft_rows() -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in store.list_flights():
            key = fleet.aircraft_key(row)
            # A flight being re-analyzed keeps its earlier result meanwhile.
            if key and (row["status"] == "done" or row["verdict"] is not None):
                groups.setdefault(key, []).append(row)
        return groups

    @app.get("/api/aircraft")
    def aircraft_list() -> dict[str, Any]:
        groups = aircraft_rows()
        items = [fleet.summarize_aircraft(k, rows) for k, rows in groups.items()]
        # most recently flown first, aircraft with drift alerts on top
        items.sort(key=lambda a: str(a["last"] or ""), reverse=True)
        items.sort(key=lambda a: len(a["alerts"]), reverse=True)
        grouped = sum(len(v) for v in groups.values())
        done = [r for r in store.list_flights() if r["status"] == "done"]
        return {
            "aircraft": items,
            "unassigned": len(done) - grouped,
            "stale": len(store.stale_flights()),
        }

    @app.get("/api/metric-specs")
    def metric_specs() -> dict[str, Any]:
        """The per-flight trend metrics: label, unit and which direction is worse."""
        return {
            "metrics": [
                {"key": m.key, "label": m.label, "unit": m.unit, "worse": m.worse} for m in METRICS
            ]
        }

    # --- threshold profiles ----------------------------------------------------------

    def requeue_aircraft(key: str) -> int:
        """Re-analyze an aircraft's flights, e.g. after its thresholds changed."""
        n = 0
        for row in aircraft_rows().get(key, []):
            if row["status"] in ("queued", "processing"):
                continue
            runner.submit(store.requeue(row["id"]), row["id"])
            n += 1
        return n

    def profile_out(name: str) -> dict[str, Any]:
        profile = store.get_profile(name)
        if profile is None:
            raise HTTPException(404, "No profile with this name")
        used_by = [k for k, p in store.profile_assignments().items() if p.name == name]
        return {**profile.to_dict(), "used_by": used_by, "defaults": _defaults_for(profile)}

    @app.get("/api/profiles")
    def profiles() -> dict[str, Any]:
        assigned = store.profile_assignments()
        return {
            "profiles": [
                {
                    **p.to_dict(),
                    "used_by": [k for k, a in assigned.items() if a.name == p.name],
                }
                for p in store.list_profiles()
            ]
        }

    @app.get("/api/profiles/{name}")
    def profile_detail(name: str) -> dict[str, Any]:
        return profile_out(name)

    @app.put("/api/profiles/{name}")
    def put_profile(name: str, body: ProfileBody) -> dict[str, Any]:
        """Create or replace a hand-written profile; aircraft using it are re-analyzed."""
        try:
            profile = parse_profile(
                {"name": name, "description": body.description, "checks": body.checks},
                source="user",
            )
        except ProfileError as exc:
            raise HTTPException(422, str(exc)) from None
        store.save_profile(profile)
        queued = sum(
            requeue_aircraft(k) for k, p in store.profile_assignments().items() if p.name == name
        )
        return {"profile": profile_out(name), "queued": queued}

    @app.delete("/api/profiles/{name}")
    def delete_profile(name: str) -> dict[str, Any]:
        """Delete a profile; aircraft that used it go back to the defaults."""
        users = [k for k, p in store.profile_assignments().items() if p.name == name]
        if not store.delete_profile(name):
            raise HTTPException(404, "No profile with this name")
        return {"deleted": name, "queued": sum(requeue_aircraft(k) for k in users)}

    @app.put("/api/aircraft/{key:path}/profile")
    def assign_profile(key: str, body: AssignBody) -> dict[str, Any]:
        if key not in aircraft_rows():
            raise HTTPException(404, "No aircraft with this id")
        if body.profile is not None and store.get_profile(body.profile) is None:
            raise HTTPException(404, "No profile with this name")
        store.assign_profile(key, body.profile)
        return {"aircraft": key, "profile": body.profile, "queued": requeue_aircraft(key)}

    @app.post("/api/aircraft/{key:path}/profile/learn")
    def learn(key: str, body: LearnBody | None = None) -> dict[str, Any]:
        """Learn attention thresholds from this aircraft's flights, assign and re-analyze."""
        rows = aircraft_rows().get(key)
        if not rows:
            raise HTTPException(404, "No aircraft with this id")
        summary = fleet.summarize_aircraft(key, rows)
        serial = (summary["serials"] or [key])[0]
        who = summary["label"] or f"{summary['hardware'] or 'board'} …{serial[-6:]}"
        name = ((body.name if body else None) or f"Learned: {who}").strip()[:NAME_MAX]
        try:
            profile = learn_profile(name, rows, notes=f"Learned from {who}'s own flights")
        except ProfileError as exc:
            raise HTTPException(409, str(exc)) from None
        store.save_profile(profile)
        store.assign_profile(key, profile.name)
        return {"profile": profile_out(profile.name), "queued": requeue_aircraft(key)}

    @app.get("/api/aircraft/{key:path}")
    def aircraft_detail(key: str) -> dict[str, Any]:
        rows = aircraft_rows().get(key)
        if not rows:
            raise HTTPException(404, "No aircraft with this id")
        ordered = sorted(rows, key=fleet.flight_time)
        assigned = store.profile_assignments().get(key)
        debriefs = store.debrief_statuses()
        return {
            **fleet.summarize_aircraft(key, rows),
            "profile": {**assigned.to_dict(), "defaults": _defaults_for(assigned)}
            if assigned
            else None,
            "profiles": [p.name for p in store.list_profiles()],
            "pending": sum(r["status"] in ("queued", "processing") for r in rows),
            "metrics": [s.to_dict() for s in fleet.series(rows)],
            "flight_list": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "filename",
                        "start_time",
                        "uploaded_at",
                        "verdict",
                        "score",
                        "firmware",
                        "serial",
                    )
                }
                | {"debrief": debriefs.get(r["id"])}
                for r in ordered
            ],
        }

    @app.get("/api/stale")
    def stale() -> dict[str, Any]:
        """Flights analyzed by an older version of a check or mapping, with the reasons."""
        details = store.stale_details()
        rows = {r["id"]: r for r in store.list_flights()}
        return {
            "flights": [
                {"id": fid, "filename": rows[fid]["filename"], "reasons": reasons}
                for fid, reasons in details.items()
                if fid in rows
            ]
        }

    @app.post("/api/reanalyze-stale", status_code=202)
    def reanalyze_stale() -> dict[str, Any]:
        """Re-analyze every flight whose analysis was made by an older check or mapping."""
        jobs = []
        for flight_id in store.stale_flights():
            job_id = store.requeue(flight_id)
            runner.submit(job_id, flight_id)
            jobs.append(job_id)
        return {"queued": len(jobs)}

    @app.post("/api/flights/{flight_id}/reanalyze", status_code=202)
    def reanalyze(flight_id: str) -> dict[str, Any]:
        row = flight_or_404(flight_id)
        if row["status"] in ("queued", "processing"):
            raise HTTPException(409, "This flight is already being analyzed")
        job_id = store.requeue(flight_id)
        runner.submit(job_id, flight_id)
        return {"flight_id": flight_id, "job_id": job_id}

    @app.delete("/api/flights/{flight_id}")
    def delete(flight_id: str) -> dict[str, Any]:
        row = flight_or_404(flight_id)
        if row["status"] in ("queued", "processing"):
            raise HTTPException(409, "This flight is being analyzed; delete it when it finishes")
        try:
            store.delete_flight(flight_id)
        except OSError as exc:
            raise HTTPException(500, f"Could not delete the flight's files: {exc}") from None
        return {"deleted": flight_id}

    @app.get("/api/flights/{flight_id}/findings")
    def findings(flight_id: str) -> dict[str, Any]:
        found = report_or_409(flight_id)["findings"]
        reviews = store.reviews_for(flight_id)
        return {
            "findings": findings_with_reviews(found, reviews),
            "orphaned_reviews": orphaned(found, reviews),
        }

    # --- engineer reviews ------------------------------------------------------------

    @app.put("/api/flights/{flight_id}/reviews/{key}")
    def put_review(flight_id: str, key: str, body: ReviewBody) -> dict[str, Any]:
        found = report_or_409(flight_id)["findings"]
        finding = next((f for f in found if finding_key(f) == key), None)
        if finding is None:
            raise HTTPException(404, "This flight has no such finding (it may have changed)")
        if finding["severity"] not in REVIEWABLE:
            raise HTTPException(422, "Only Problem and Attention findings can be reviewed")
        if body.status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        store.set_review(flight_id, finding, body.status, body.note.strip()[:NOTE_MAX] or None)
        return {"review": store.reviews_for(flight_id)[key]}

    @app.delete("/api/flights/{flight_id}/reviews/{key}")
    def delete_review(flight_id: str, key: str) -> dict[str, Any]:
        flight_or_404(flight_id)
        if not store.clear_review(flight_id, key):
            raise HTTPException(404, "No review for this finding")
        return {"deleted": key}

    # --- notes and debrief -----------------------------------------------------------

    def note_or_404(note_id: str) -> dict[str, Any]:
        note = store.get_note(note_id)
        if note is None:
            raise HTTPException(404, "No note with this id")
        return note

    def check_view(view: dict[str, Any] | None) -> None:
        if view is not None and len(json.dumps(view)) > 2000:
            raise HTTPException(422, "The saved view is too large")

    @app.get("/api/flights/{flight_id}/notes")
    def notes(flight_id: str) -> dict[str, Any]:
        flight_or_404(flight_id)
        return {"notes": store.list_notes(flight_id)}

    @app.post("/api/flights/{flight_id}/notes", status_code=201)
    def add_note(flight_id: str, body: NoteBody) -> dict[str, Any]:
        """A time-stamped note (a moment, or a range with ``t_end``) about the flight."""
        flight_or_404(flight_id)
        if body.t_end is not None and body.t_end < body.t_start:
            raise HTTPException(422, "t_end is before t_start")
        check_view(body.view)
        note = body.model_dump()
        note["text"] = note["text"].strip()
        note["tags"] = sorted({t.strip().lower() for t in note["tags"] if t.strip()})
        return {"note": store.add_note(flight_id, note)}

    @app.patch("/api/notes/{note_id}")
    def patch_note(note_id: str, body: NotePatch) -> dict[str, Any]:
        note = note_or_404(note_id)
        changes = body.model_dump(exclude_unset=True)
        start = changes.get("t_start", note["t_start"])
        end = changes.get("t_end", note["t_end"])
        if start is None:
            raise HTTPException(422, "t_start cannot be removed")
        if end is not None and end < start:
            raise HTTPException(422, "t_end is before t_start")
        check_view(changes.get("view"))
        if "text" in changes:
            if changes["text"] is None or not changes["text"].strip():
                raise HTTPException(422, "A note needs text")
            changes["text"] = changes["text"].strip()
        if "tags" in changes:
            changes["tags"] = sorted(
                {t.strip().lower() for t in changes["tags"] or [] if t.strip()}
            )
        return {"note": store.update_note(note_id, changes)}

    @app.delete("/api/notes/{note_id}")
    def delete_note(note_id: str) -> dict[str, Any]:
        note_or_404(note_id)
        store.delete_note(note_id)
        return {"deleted": note_id}

    @app.get("/api/flights/{flight_id}/debrief")
    def debrief(flight_id: str) -> dict[str, Any]:
        flight_or_404(flight_id)
        return {"debrief": store.get_debrief(flight_id)}

    @app.put("/api/flights/{flight_id}/debrief")
    def put_debrief(flight_id: str, body: DebriefBody) -> dict[str, Any]:
        """The flight's debrief: status (open or closed), summary, conclusion, actions."""
        flight_or_404(flight_id)
        d = body.model_dump()
        d["summary"] = d["summary"].strip() or None
        d["conclusion"] = d["conclusion"].strip() or None
        d["actions"] = [a.strip() for a in d["actions"] if a.strip()]
        return {"debrief": store.put_debrief(flight_id, d)}

    @app.get("/api/reviews")
    def reviews() -> dict[str, Any]:
        return {"reviews": store.all_reviews()}

    @app.get("/api/reviews/manifest.yaml")
    def reviews_manifest() -> Response:
        """Reviews as a ``flightlens validate`` manifest (a regression test set)."""
        text = manifest_yaml(store.all_reviews(), store.root.resolve() / "flights")
        return Response(
            text,
            media_type="application/yaml",
            headers={"Content-Disposition": _attachment("flightlens-reviews.yaml")},
        )

    @app.get("/api/flights/{flight_id}/events")
    def events(flight_id: str) -> dict[str, Any]:
        r = report_or_409(flight_id)
        return {"events": r["events"], "segments": r["segments"]}

    @app.get("/api/flights/{flight_id}/signals")
    def signals(
        flight_id: str,
        names: Annotated[str, Query(description="Comma-separated signal names")],
        start: float | None = None,
        end: float | None = None,
        max_points: Annotated[int, Query(ge=10, le=MAX_POINTS)] = 2000,
    ) -> dict[str, Any]:
        catalog = {s["name"]: s for s in report_or_409(flight_id)["signals"]}
        out = []
        for name in [n.strip() for n in names.split(",") if n.strip()][:24]:
            if name not in catalog:
                out.append({"name": name, "missing": True})
                continue
            data = store.read_signal(flight_id, name)
            if data is None:
                out.append({"name": name, "missing": True})
                continue
            t, v = data
            if start is not None or end is not None:
                sel = np.ones(t.shape, dtype=bool)
                if start is not None:
                    sel &= t >= start
                if end is not None:
                    sel &= t <= end
                t, v = t[sel], v[sel]
            total = int(t.size)
            t, v = lttb(t, v, max_points)
            ts, vs = with_gaps(t, v)
            out.append(
                {
                    "name": name,
                    "unit": catalog[name]["unit"],
                    "source": catalog[name]["source"],
                    "samples": total,
                    "t": ts,
                    "v": vs,
                }
            )
        return {"signals": out}

    @app.get("/api/flights/{flight_id}/tuning")
    def tuning(flight_id: str) -> dict[str, Any]:
        """Rate-loop step response per axis (median and spread), or null."""
        report_or_409(flight_id)
        return {"tuning": store.read_tuning(flight_id)}

    @app.get("/api/flights/{flight_id}/spectrum")
    def spectrum(flight_id: str) -> dict[str, Any]:
        """Gyro noise spectrum (PSD per axis, throttle tracking, spectrogram), or null."""
        report_or_409(flight_id)
        return {"spectrum": store.read_spectrum(flight_id)}

    # --- videos ------------------------------------------------------------------

    def video_or_404(video_id: str) -> dict[str, Any]:
        video = store.get_video(video_id)
        if video is None:
            raise HTTPException(404, "No video with this id")
        return video

    @app.post("/api/flights/{flight_id}/videos", status_code=201)
    def upload_video(flight_id: str, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        """Attach a video (MP4 or WebM) to a flight. Stored as uploaded, with its SHA-256."""
        flight_or_404(flight_id)
        if disk_free() < min_free_bytes:
            raise HTTPException(507, "The server is low on disk space; uploads are paused")
        filename = Path(file.filename or "video").name[:200]
        tmp = store.root / "uploads" / f"{uuid.uuid4().hex}.video.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("wb") as fh:
                while chunk := file.file.read(CHUNK):
                    size += len(chunk)
                    if size > max_video_bytes:
                        raise HTTPException(
                            413, f"The video is larger than the {_size(max_video_bytes)} limit"
                        )
                    digest.update(chunk)
                    fh.write(chunk)
            if size == 0:
                raise HTTPException(400, "The file is empty")
            try:
                ext = media.sniff(tmp)
            except media.MediaError as exc:
                raise HTTPException(415, str(exc)) from None
            sha = digest.hexdigest()
            existing = next((v for v in store.list_videos(flight_id) if v["sha256"] == sha), None)
            if existing is not None:
                return {"video": existing, "duplicate": True}
            info = media.probe(tmp)
            video_id = uuid.uuid4().hex[:16]
            folder = store.videos_dir(flight_id)
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"{video_id}.{ext}"
            shutil.move(str(tmp), dest)
            dest.chmod(0o444)  # the original is evidence, like the log
            row = {
                "id": video_id,
                "filename": filename,
                "file": dest.name,
                "sha256": sha,
                "size_bytes": size,
                **info.as_row(),
                "uploaded_at": time.time(),
            }
            video = store.add_video(flight_id, row)
            return {"video": video, "duplicate": False, "browser_codec": _playable(info.codec)}
        finally:
            tmp.unlink(missing_ok=True)

    @app.get("/api/flights/{flight_id}/videos")
    def videos(flight_id: str) -> dict[str, Any]:
        flight_or_404(flight_id)
        return {"videos": store.list_videos(flight_id)}

    @app.get("/api/videos/{video_id}/file")
    def video_file(video_id: str) -> FileResponse:
        """The video as uploaded; answers HTTP Range requests, so the player can seek."""
        video = video_or_404(video_id)
        path = store.video_path(video)
        if not path.is_file():
            raise HTTPException(404, "The video file is missing")
        ext = path.suffix.lstrip(".")
        return FileResponse(path, media_type=media.MEDIA_TYPES.get(ext, "application/octet-stream"))

    @app.patch("/api/videos/{video_id}")
    def patch_video(video_id: str, body: VideoPatch) -> dict[str, Any]:
        """Sync settings (offset, rate, method, points), label, or the duration the browser read."""
        video_or_404(video_id)
        changes = body.model_dump(exclude_unset=True)
        if "sync_points" in changes:
            changes["sync_points"] = [list(p) for p in changes["sync_points"]]
        return {"video": store.update_video(video_id, changes)}

    @app.delete("/api/videos/{video_id}")
    def delete_video(video_id: str) -> dict[str, Any]:
        video_or_404(video_id)
        store.delete_video(video_id)
        return {"deleted": video_id}

    def replay_or_409(flight_id: str) -> dict[str, Any]:
        report_or_409(flight_id)
        meta = store.read_replay_meta(flight_id)
        if meta is None:
            raise HTTPException(
                409, "This flight was analyzed before Replay existed; re-analyze it to replay it"
            )
        return meta

    @app.get("/api/flights/{flight_id}/replay")
    def replay(flight_id: str) -> dict[str, Any]:
        """Replay metadata: origin, rate, columns, modes, gaps, data sources.
        The table itself is ``replay.bin``."""
        return replay_or_409(flight_id)

    @app.get("/api/flights/{flight_id}/replay.bin")
    def replay_table(flight_id: str, request: Request) -> Response:
        """The replay table: for each column in ``columns`` order, ``rows`` little-endian
        float32 values (NaN = no data), gzip-compressed when the client accepts it."""
        meta = replay_or_409(flight_id)
        table = store.read_replay_table(flight_id)
        if table is None:
            raise HTTPException(409, "The replay data is missing; re-analyze the flight")
        body = b"".join(
            np.ascontiguousarray(table[c], dtype="<f4").tobytes() for c in meta["columns"]
        )
        headers = {"Cache-Control": "no-cache", "X-Replay-Rows": str(meta["rows"])}
        if "gzip" in request.headers.get("accept-encoding", ""):
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        return Response(body, media_type="application/octet-stream", headers=headers)

    @app.get("/api/flights/{flight_id}/track")
    def track(flight_id: str) -> dict[str, Any]:
        report_or_409(flight_id)
        for prefix in ("pos", "gps"):
            lat = store.read_signal(flight_id, f"{prefix}.lat")
            lon = store.read_signal(flight_id, f"{prefix}.lon")
            if lat is None or lon is None or lat[0].size != lon[0].size:
                continue
            t, la, lo = lat[0], lat[1], lon[1]
            ok = np.isfinite(la) & np.isfinite(lo)
            t, la, lo = t[ok], la[ok], lo[ok]
            if t.size < 2:
                continue
            alt_sig = store.read_signal(flight_id, "pos.rel_alt")
            alt = (
                np.interp(t, alt_sig[0], alt_sig[1])
                if alt_sig is not None and alt_sig[0].size > 1
                else np.full(t.shape, np.nan)
            )
            step = max(1, t.size // TRACK_POINTS)
            sl = slice(None, None, step)
            return {
                "source": "estimated position" if prefix == "pos" else "GPS receiver",
                "datum": "WGS84; altitude relative to the estimator origin",
                "t": t[sl].tolist(),
                "lat": la[sl].tolist(),
                "lon": lo[sl].tolist(),
                "alt": [None if not np.isfinite(a) else float(a) for a in alt[sl]],
            }
        return {"source": None, "t": [], "lat": [], "lon": [], "alt": []}

    @app.get("/api/flights/{flight_id}/report.json")
    def report_json(flight_id: str) -> JSONResponse:
        report = report_or_409(flight_id)
        name = Path(flight_or_404(flight_id)["filename"]).stem + ".report.json"
        return JSONResponse(report, headers={"Content-Disposition": _attachment(name)})

    @app.get("/api/flights/{flight_id}/report.html")
    def report_html(flight_id: str, download: bool = False) -> HTMLResponse:
        """The printable report (self-contained HTML; print to PDF from the browser)."""
        report = report_or_409(flight_id)
        row = flight_or_404(flight_id)
        units = {s["name"]: s["unit"] for s in report.get("signals", [])}

        def read(name: str) -> tuple[np.ndarray, np.ndarray, str] | None:
            if name not in units:
                return None
            data = store.read_signal(flight_id, name)
            return (data[0], data[1], units[name]) if data is not None else None

        html = render_html(
            report,
            read,
            filename=row["filename"],
            aircraft=row.get("aircraft"),
            reviews=store.reviews_for(flight_id),
            stale=row_stale(row),
        )
        headers = {
            # the report fetches nothing and runs no script but its print button
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
        }
        if download:
            headers["Content-Disposition"] = _attachment(
                Path(row["filename"]).stem + ".report.html"
            )
        return HTMLResponse(html, headers=headers)

    @app.get("/api/flights/{flight_id}/debrief.html")
    def debrief_html(flight_id: str, download: bool = False) -> HTMLResponse:
        """The printable debrief report: debrief, timeline with notes, track, provenance."""
        report = report_or_409(flight_id)
        row = flight_or_404(flight_id)
        html_text = render_debrief(
            report,
            filename=row["filename"],
            aircraft=row.get("aircraft"),
            debrief=store.get_debrief(flight_id),
            notes=store.list_notes(flight_id),
            reviews=store.reviews_for(flight_id),
            videos=store.list_videos(flight_id),
            replay_cols=store.read_replay_table(flight_id),
            replay_meta=store.read_replay_meta(flight_id),
        )
        headers = {
            # like the health report: nothing fetched, no script but the print button
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:",
            "X-Content-Type-Options": "nosniff",
        }
        if download:
            headers["Content-Disposition"] = _attachment(
                Path(row["filename"]).stem + ".debrief.html"
            )
        return HTMLResponse(html_text, headers=headers)

    @app.get("/api/flights/{flight_id}/original")
    def original(flight_id: str) -> FileResponse:
        row = flight_or_404(flight_id)
        path = store.original_path(flight_id)
        if path is None:
            raise HTTPException(404, "The original file is missing")
        return FileResponse(path, filename=row["filename"])

    # --- web UI ------------------------------------------------------------------

    if web_dir is not None and (web_dir / "index.html").exists():
        page_csp = spa_csp((web_dir / "index.html").read_text(encoding="utf-8"))
        assets = web_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404, "Unknown API endpoint")
            candidate = (web_dir / path).resolve()
            if path and candidate.is_file() and web_dir.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(
                web_dir / "index.html", headers={"Content-Security-Policy": page_csp}
            )

    return app


def spa_csp(index_html: str) -> str:
    """Content-Security-Policy for the web UI: its own scripts, plus each inline
    script of ``index.html`` by hash (the theme is applied before first paint)."""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.encode()).digest()).decode() + "'"
        for m in re.findall(r"<script>(.*?)</script>", index_html, flags=re.DOTALL)
    )
    return (
        f"default-src 'self'; script-src 'self' {hashes}".rstrip()
        + "; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


def _attachment(filename: str) -> str:
    """Content-Disposition for any file name (RFC 6266 with an ASCII fallback)."""
    fallback = "".join(c for c in filename if c.isascii() and c.isprintable() and c not in '"\\')
    return (
        f"attachment; filename=\"{fallback or 'report.json'}\"; filename*=UTF-8''{quote(filename)}"
    )


def _defaults_for(profile: Any) -> dict[str, dict[str, float]]:
    """The checks' default values for each setting a profile overrides."""
    by_id = {c.id: c for c in all_checks()}
    return {
        cid: {k: by_id[cid].defaults[k] for k in values if cid in by_id}
        for cid, values in profile.overrides.items()
    }


class ObservedRequests:
    """Times, counts and logs every request, and tags it with an X-Request-ID.

    Metrics use the route *template* (``/api/flights/{flight_id}``), never the
    raw path, so the number of series stays small.
    """

    def __init__(self, app: Any, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next((v for k, v in scope["headers"] if k == b"x-request-id"), None)
        rid = request_id(incoming.decode("latin-1") if incoming else None)
        status = 500
        started = time.perf_counter()

        async def tagged(message: Any) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in SECURITY_HEADERS if k not in present]
                message["headers"] = [*headers, (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, tagged)
        finally:
            seconds = time.perf_counter() - started
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            labels = {"method": scope["method"], "route": template, "status": str(status)}
            self.metrics.inc("flightlens_http_requests_total", labels)
            self.metrics.observe(
                "flightlens_http_request_duration_seconds", seconds, {"route": template}
            )
            path = scope["path"]
            quiet = template in QUIET_ROUTES or path.startswith("/assets/")
            level = logging.ERROR if status >= 500 else logging.DEBUG if quiet else logging.INFO
            http_log.log(
                level,
                "request",
                extra={
                    "method": scope["method"],
                    "path": path,
                    "route": template,
                    "status": status,
                    "ms": round(seconds * 1000, 1),
                    "request_id": rid,
                },
            )


class RequestGuard:
    """Checks every request before the app sees it.

    * The Host header must be an allowed host (DNS-rebinding protection).
    * Requests that change data must not come from another site: if the browser
      sends an Origin header, it must match the Host.
    * On a read-only server, nothing may change.
    * Uploads must declare their size, and it must be within the limit, so an
      oversized body is refused before anything is written to disk.
    """

    def __init__(
        self,
        app: Any,
        allowed_hosts: tuple[str, ...],
        read_only: bool = False,
        upload_refusal: Any = None,
        limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.read_only = read_only
        self.upload_refusal = upload_refusal
        self.limits = limits or {"log": MAX_UPLOAD_BYTES, "video": MAX_VIDEO_BYTES}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "")
        if "*" not in self.allowed_hosts and _hostname(host) not in self.allowed_hosts:
            await _reject(send, 400, "Unknown host. Start the server with --allow-host to use it.")
            return

        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            if self.read_only:
                await _reject(send, 403, "This is a read-only demo: uploads and changes are off")
                return
            origin = headers.get("origin")
            if origin is not None and urlsplit(origin).netloc.lower() != host.lower():
                await _reject(send, 403, "Cross-site requests are not allowed")
                return

        kind = next(
            (k for rx, k in UPLOAD_ROUTES if scope["method"] == "POST" and rx.match(scope["path"])),
            None,
        )
        if kind is not None:
            try:
                length = int(headers["content-length"])
            except (KeyError, ValueError):
                await _reject(send, 411, "The upload must declare its size (Content-Length)")
                return
            limit = self.limits[kind]
            if length > limit + MULTIPART_OVERHEAD:
                await _reject(send, 413, f"The file is larger than the {_size(limit)} limit")
                return
            refusal = self.upload_refusal(length, kind) if self.upload_refusal else None
            if refusal is not None:
                await _reject(send, refusal[0], refusal[1], refusal[2])
                return
            receive = _limited(receive, length)

        await self.app(scope, receive, send)


def _playable(codec: str | None) -> bool | None:
    """Whether every common browser plays this codec (None: unknown without ffprobe)."""
    return None if codec is None else codec in media.BROWSER_CODECS


def _size(n: int) -> str:
    return f"{n / 1024**3:.3g} GB" if n >= 1024**3 else f"{n / 1024**2:.3g} MB"


def _hostname(host: str) -> str:
    """'localhost:8000' -> 'localhost', '[::1]:8000' -> '::1'."""
    host = host.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _limited(receive: Any, limit: int) -> Any:
    """Stop reading a body that sends more than it declared."""
    seen = 0

    async def wrapped() -> Any:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                return {"type": "http.disconnect"}
        return message

    return wrapped


async def _reject(
    send: Any, status: int, detail: str, headers: dict[str, str] | None = None
) -> None:
    body = json.dumps({"detail": detail}).encode()
    extra = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _with_name(tmp: Path, filename: str) -> Path:
    """The temp upload renamed with the original extension (detection uses it for .tlog)."""
    target = tmp.with_suffix(safe_suffix(filename))
    if tmp.exists() and not target.exists():
        tmp.replace(target)
    return target
