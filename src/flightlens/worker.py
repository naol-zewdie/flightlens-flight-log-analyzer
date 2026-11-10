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
from pathlib import Path
from typing import Any

from flightlens.store import Store, write_signals

DEFAULT_TIMEOUT_S = 300


def process_log(log_path: str, out_dir: str) -> dict[str, Any]:
    """Parse, analyze and write results. Runs in the child process."""
    from collections import Counter

    from flightlens import parsers
    from flightlens.engine import analyze
    from flightlens.model import Severity

    flight = parsers.load(Path(log_path))
    report = analyze(flight)
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


def _child(log_path: str, out_dir: str, conn: Any) -> None:
    try:
        conn.send(("ok", process_log(log_path, out_dir)))
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
            args=(str(original), str(self.store.flight_dir(flight_id)), child),
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
