"""Background processing of uploaded logs.

Each log is parsed and analyzed in its own child process with a time limit,
so a malicious, corrupt or huge file cannot crash or hang the web server.
Results are written to a temporary folder and renamed into place, so a retried
or interrupted job never leaves half-written results.
"""

from __future__ import annotations

import json
import math
import multiprocessing as mp
import queue
import shutil
import threading
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flightlens.profiles import Profile
from flightlens.store import Store, write_signals

DEFAULT_TIMEOUT_S = 300


@dataclass(frozen=True)
class ProfileContext:
    """What the child process needs to find a flight's threshold profile.

    The board serial is only known after parsing, so the profile is resolved
    there: the flight's own label first, then the label other flights of the
    same board carry, then the board serial itself.
    """

    assignments: Mapping[str, Profile] = field(default_factory=dict)
    label: str | None = None
    labels_by_serial: Mapping[str, str] = field(default_factory=dict)

    def resolve(self, serial: str | None) -> Profile | None:
        keys = []
        if self.label:
            keys.append(f"label:{self.label}")
        if serial and serial in self.labels_by_serial:
            keys.append(f"label:{self.labels_by_serial[serial]}")
        if serial:
            keys.append(f"sn:{serial}")
        return next((self.assignments[k] for k in keys if k in self.assignments), None)

    @classmethod
    def from_store(cls, store: Store, flight_id: str) -> ProfileContext:
        assignments = store.profile_assignments()
        if not assignments:
            return cls()
        row = store.get_flight(flight_id) or {}
        return cls(assignments, row.get("aircraft"), store.labels_by_serial())


def process_log(
    log_path: str, out_dir: str, profiles: ProfileContext | None = None
) -> dict[str, Any]:
    """Parse, analyze and write results. Runs in the child process."""
    from collections import Counter

    from flightlens import parsers
    from flightlens.engine import analyze
    from flightlens.metrics import flight_metrics
    from flightlens.model import Severity
    from flightlens.spectrum import analyze_spectrum

    flight = parsers.load(Path(log_path))
    profile = profiles.resolve(flight.metadata.serial) if profiles else None
    report = analyze(flight, profile=profile)
    out = Path(out_dir)
    if not out.is_dir():
        raise FileNotFoundError("The flight was deleted while it was being analyzed")
    tmp = out / "analysis.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    write_signals(tmp / "signals", flight.signals)

    data = report.to_dict()
    data["signals"] = [
        {
            "name": s.name,
            "unit": s.unit,
            "source": s.source,
            "samples": len(s),
            "rate_hz": round((len(s) - 1) / float(s.t[-1] - s.t[0]), 2)
            if len(s) > 1 and s.t[-1] > s.t[0]
            else None,
        }
        for s in sorted(flight.signals.values(), key=lambda s: s.name)
    ]
    data["events"] = [
        {"t": e.t, "kind": e.kind.value, "text": e.text, "level": e.level, "source": e.source}
        for e in flight.events
    ]
    data["segments"] = [{"start": s.start, "end": s.end} for s in flight.segments]
    (tmp / "report.json").write_text(json.dumps(_clean(data), allow_nan=False), encoding="utf-8")
    spec = analyze_spectrum(flight)
    if spec is not None:
        (tmp / "spectrum.json").write_text(
            json.dumps(_clean(spec.to_dict()), allow_nan=False), encoding="utf-8"
        )

    # Swap in the new results: the old folder is renamed aside (fast, and works
    # on Windows even if a reader has just opened a file), then removed.
    final, old = out / "analysis", out / "analysis.old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.replace(old)
    tmp.replace(final)
    shutil.rmtree(old, ignore_errors=True)

    counts = Counter(f.severity for f in report.findings)
    md = flight.metadata
    return {
        "firmware": md.firmware,
        "vehicle_type": md.vehicle_type,
        "hardware": md.hardware,
        "serial": md.serial,
        "metrics": flight_metrics(flight, report),
        "start_time": md.start_time.isoformat() if md.start_time else None,
        "duration_s": data["summary"]["duration_s"],
        "verdict": report.verdict.value,
        "score": report.score,
        "n_problem": counts[Severity.PROBLEM],
        "n_attention": counts[Severity.ATTENTION],
    }


def _clean(obj: Any) -> Any:
    """Make data strict-JSON safe: NaN and infinity become null."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_clean(v) for v in obj]
    return obj


def _child(log_path: str, out_dir: str, profiles: ProfileContext, conn: Any) -> None:
    try:
        conn.send(("ok", process_log(log_path, out_dir, profiles)))
    except Exception as exc:
        from flightlens.parsers import ParseError

        if isinstance(exc, ParseError):
            conn.send(("rejected", str(exc)))
        else:
            conn.send(("crash", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"))
    finally:
        conn.close()


class JobRunner:
    """Processes queued jobs one at a time on a background thread."""

    def __init__(self, store: Store, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.store = store
        self.timeout_s = timeout_s
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._ctx = mp.get_context("spawn")
        self._thread = threading.Thread(target=self._loop, name="flightlens-jobs", daemon=True)
        self._thread.start()

    def submit(self, job_id: str, flight_id: str) -> None:
        self._queue.put((job_id, flight_id))

    def pending(self) -> int:
        return self._queue.qsize()

    def _loop(self) -> None:
        while True:
            job_id, flight_id = self._queue.get()
            try:
                self._run(job_id, flight_id)
            except Exception as exc:  # never let the loop die
                self.store.set_job(job_id, "failed", f"internal error: {exc}")

    def _run(self, job_id: str, flight_id: str) -> None:
        original = self.store.original_path(flight_id)
        if original is None:
            self.store.set_job(job_id, "failed", "The original log file is missing")
            return
        self.store.set_job(job_id, "processing")
        parent, child = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(
            target=_child,
            args=(
                str(original),
                str(self.store.flight_dir(flight_id)),
                ProfileContext.from_store(self.store, flight_id),
                child,
            ),
            daemon=True,
        )
        proc.start()
        child.close()
        if not parent.poll(self.timeout_s):
            proc.kill()
            proc.join()
            self.store.set_job(
                job_id,
                "failed",
                f"Analysis took longer than {self.timeout_s:.0f} s and was stopped",
            )
            return
        try:
            status, payload = parent.recv()
        except EOFError:
            status, payload = "crash", "The analysis process exited unexpectedly"
        proc.join(timeout=10)

        if status == "ok":
            self.store.record_result(flight_id, payload)
            self.store.set_job(job_id, "done")
        elif status == "rejected":
            self.store.set_job(job_id, "failed", f"Cannot read this log: {payload}")
        else:
            self.store.set_job(
                job_id, "failed", f"Analysis error: {(str(payload).splitlines() or ['unknown'])[0]}"
            )
