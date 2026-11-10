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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from flightlens import __version__, parsers
from flightlens.lttb import lttb, with_gaps
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


def create_app(
    data_dir: Path,
    web_dir: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
) -> FastAPI:
    """Build the app.

    ``allowed_hosts`` lists the host names the server answers to (``"*"`` for
    any). Anything else is refused, which blocks DNS-rebinding attacks from web
    pages open in the user's browser.
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
    app.add_middleware(RequestGuard, allowed_hosts=tuple(h.lower() for h in allowed_hosts))

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
        return {"status": "ok", "version": __version__, "queued_jobs": runner.pending()}

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
        return {"flights": store.list_flights(verdict=verdict, q=q)}

    @app.get("/api/flights/{flight_id}")
    def flight(flight_id: str) -> dict[str, Any]:
        row = flight_or_404(flight_id)
        report = store.read_report(flight_id)
        if report is None:
            return {"flight": row, "report": None}
        slim = {k: v for k, v in report.items() if k not in ("findings", "events")}
        return {"flight": row, "report": slim}

    @app.patch("/api/flights/{flight_id}")
    def patch_flight(flight_id: str, body: FlightPatch) -> dict[str, Any]:
        flight_or_404(flight_id)
        label = (body.aircraft or "").strip()[:80] or None
        store.set_aircraft(flight_id, label)
        return {"flight": store.get_flight(flight_id)}

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
        return {"findings": report_or_409(flight_id)["findings"]}

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


class RequestGuard:
    """Checks every request before the app sees it.

    * The Host header must be an allowed host (DNS-rebinding protection).
    * Requests that change data must not come from another site: if the browser
      sends an Origin header, it must match the Host.
    * Uploads must declare their size, and it must be within the limit, so an
      oversized body is refused before anything is written to disk.
    """

    def __init__(self, app: Any, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

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
