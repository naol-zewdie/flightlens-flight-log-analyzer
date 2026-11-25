"""FlightLens read-only public demo, as a Vercel Python function.

The demo library is built at deploy time (``build.py``) and shipped in the
function bundle. The bundle is read-only, so on a cold start the library is
copied to ``/tmp`` (SQLite needs a writable folder) and served with every
change refused: uploads, edits, reviews, notes, debriefs and deletes.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

import flightlens
from flightlens.api import LOCAL_HOSTS, create_app

HERE = Path(__file__).resolve().parent
BUILT = HERE / "demo-data"
# the web UI bundled in the installed package ($FLIGHTLENS_WEB for a local try)
WEB = Path(os.environ.get("FLIGHTLENS_WEB") or Path(flightlens.__file__).resolve().parent / "web")


def _library() -> Path:
    """A writable copy of the demo library (one per function instance)."""
    target = Path(tempfile.gettempdir()) / "flightlens-demo"
    if not (target / "flightlens.db").exists():
        partial = target.with_name(target.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(BUILT, partial)
        shutil.rmtree(target, ignore_errors=True)
        partial.rename(target)  # a copy cut short by a timeout is never used
    return target


def _hosts() -> list[str]:
    """This deployment's own host names, plus any custom domain in FLIGHTLENS_ALLOWED_HOSTS."""
    names = [
        os.environ.get(v, "")
        for v in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL")
    ]
    names += os.environ.get("FLIGHTLENS_ALLOWED_HOSTS", "").split(",")
    return [*LOCAL_HOSTS, *(n.strip().lower() for n in names if n.strip())]


# min_free_bytes=0: the free-space check guards uploads, which a read-only demo
# never takes, and /tmp on Vercel is small (health would always say "degraded")
app = create_app(
    _library(), WEB, allowed_hosts=_hosts(), read_only=True, workers=1, min_free_bytes=0
)

# Start the (idle) job workers now rather than in the ASGI lifespan, which a
# serverless runtime may not run; health reports them. Nothing is ever queued:
# the demo is read-only.
app.state.runner.start()


@asynccontextmanager
async def _no_lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


app.router.lifespan_context = _no_lifespan
