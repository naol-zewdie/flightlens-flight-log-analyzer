"""HTTP API and static web UI.

All endpoints are under ``/api``. The built web UI (``web/dist``) is served at
``/`` when present. Uploads are streamed to disk, hashed, format-checked and
queued; parsing happens in an isolated child process (see ``worker.py``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from flightlens import __version__, fleet, parsers
from flightlens.checks import all_checks
from flightlens.htmlreport import render_html
from flightlens.lttb import lttb, with_gaps
from flightlens.metrics import METRICS
from flightlens.profiles import NAME_MAX, ProfileError, learn_profile, parse_profile
from flightlens.provenance import stale_reasons
from flightlens.reviews import NOTE_MAX, REVIEWABLE, STATUSES, finding_key, manifest_yaml, orphaned
from flightlens.reviews import with_keys as findings_with_reviews
from flightlens.store import Store, safe_suffix
from flightlens.worker import DEFAULT_TIMEOUT_S, JobRunner

MAX_UPLOAD_BYTES = 1024**3  # 1 GB, from the spec
MULTIPART_OVERHEAD = 1 << 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
CHUNK = 1 << 20
MAX_POINTS = 20_000
TRACK_POINTS = 3_000


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
) -> FastAPI:
    """Build the app.

    ``allowed_hosts`` lists the host names the server answers to (``"*"`` for
    any). Anything else is refused, which blocks DNS-rebinding attacks from web
    pages open in the user's browser. ``read_only`` refuses every change
    (uploads, edits, deletes, re-analysis), for a public demo.
    """
    store = Store(data_dir)
    runner = JobRunner(store, timeout_s=timeout_s)
    app = FastAPI(
        title="FlightLens",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.store = store
    app.state.runner = runner
    app.add_middleware(
        RequestGuard, allowed_hosts=tuple(h.lower() for h in allowed_hosts), read_only=read_only
    )

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
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "queued_jobs": runner.pending(),
            "read_only": read_only,
        }

    # A plain ``def``: FastAPI runs it in a worker thread, so hashing and
    # copying a large file does not block other requests.
    @app.post("/api/logs", status_code=202)
    def upload(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
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
    def flights(verdict: str | None = None, q: str | None = None) -> dict[str, Any]:
        reviewed = store.review_counts()
        return {
            "flights": [
                {**row, "stale": bool(row_stale(row)), "reviewed": reviewed.get(row["id"], 0)}
                for row in store.list_flights(verdict=verdict, q=q)
            ]
        }

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

    @app.get("/api/flights/{flight_id}/original")
    def original(flight_id: str) -> FileResponse:
        row = flight_or_404(flight_id)
        path = store.original_path(flight_id)
        if path is None:
            raise HTTPException(404, "The original file is missing")
        return FileResponse(path, filename=row["filename"])

    # --- web UI ------------------------------------------------------------------

    if web_dir is not None and (web_dir / "index.html").exists():
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
            return FileResponse(web_dir / "index.html")

    return app


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


class RequestGuard:
    """Checks every request before the app sees it.

    * The Host header must be an allowed host (DNS-rebinding protection).
    * Requests that change data must not come from another site: if the browser
      sends an Origin header, it must match the Host.
    * On a read-only server, nothing may change.
    * Uploads must declare their size, and it must be within the limit, so an
      oversized body is refused before anything is written to disk.
    """

    def __init__(self, app: Any, allowed_hosts: tuple[str, ...], read_only: bool = False) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.read_only = read_only

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

        if scope["method"] == "POST" and scope["path"] == "/api/logs":
            try:
                length = int(headers["content-length"])
            except (KeyError, ValueError):
                await _reject(send, 411, "The upload must declare its size (Content-Length)")
                return
            if length > MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD:
                await _reject(send, 413, "The file is larger than the 1 GB limit")
                return
            receive = _limited(receive, length)

        await self.app(scope, receive, send)


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


async def _reject(send: Any, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
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
