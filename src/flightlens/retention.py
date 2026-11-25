"""Retention: removing old flights from a library.

Nothing is removed automatically. ``flightlens prune --older-than DAYS`` lists
the flights older than that (by the flight's own date when the log has one,
else by when it was uploaded) and deletes them only with ``--yes``. Flights an
engineer has reviewed, annotated or debriefed are kept by default: that work is evidence, and it is
the regression labels ``flightlens validate`` runs on.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flightlens.store import Store

DAY = 86_400.0


@dataclass(frozen=True)
class Candidate:
    id: str
    filename: str
    when: float
    """Unix time of the flight (log date, else upload)."""
    size_bytes: int


def flight_time(row: dict[str, Any]) -> float:
    if row.get("start_time"):
        try:
            return datetime.fromisoformat(str(row["start_time"])).timestamp()
        except ValueError:
            pass
    return float(row["uploaded_at"])


def candidates(
    store: Store, older_than_days: float, keep_reviewed: bool = True, now: float | None = None
) -> list[Candidate]:
    """Flights older than ``older_than_days``, oldest first (never ones being analyzed)."""
    cutoff = (now if now is not None else time.time()) - older_than_days * DAY
    # an engineer's work on a flight (reviews, notes, a debrief) keeps it
    reviewed: set[str] = set()
    if keep_reviewed:
        reviewed = (
            set(store.review_counts()) | set(store.note_counts()) | set(store.debrief_statuses())
        )
    video_bytes = store.video_bytes()
    out = []
    for row in store.list_flights():
        if row["status"] in ("queued", "processing") or row["id"] in reviewed:
            continue
        when = flight_time(row)
        if when < cutoff:
            size = int(row["size_bytes"] or 0) + video_bytes.get(row["id"], 0)
            out.append(Candidate(row["id"], row["filename"], when, size))
    return sorted(out, key=lambda c: c.when)


def prune(store: Store, chosen: list[Candidate]) -> int:
    """Delete the given flights (files and index); returns how many were removed."""
    n = 0
    for c in chosen:
        if store.delete_flight(c.id):
            n += 1
    return n
