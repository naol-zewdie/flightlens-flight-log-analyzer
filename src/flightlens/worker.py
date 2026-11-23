"""Background processing of uploaded logs.

Each log is parsed and analyzed in its own child process with a time limit,
so a malicious, corrupt or huge file cannot crash or hang the web server.
Results are written to a temporary folder and renamed into place, so a retried
or interrupted job never leaves half-written results.
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing as mp
import queue
import shutil
import signal
import threading
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flightlens.ops import JOB_BUCKETS, Metrics
from flightlens.profiles import Profile
from flightlens.store import Store, write_signals

DEFAULT_TIMEOUT_S = 300
DEFAULT_WORKERS = 1
DEFAULT_MAX_QUEUE = 500
MAX_ATTEMPTS = 3
"""A job interrupted this many times by a server stop is failed, not resumed again."""

log = logging.getLogger("flightlens.jobs")


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
    from flightlens.provenance import versions_of
    from flightlens.spectrum import analyze_spectrum
    from flightlens.tuning import analyze_tuning

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
    tuning = analyze_tuning(flight)
    if tuning is not None:
        (tmp / "tuning.json").write_text(
            json.dumps(_clean(tuning.to_dict()), allow_nan=False), encoding="utf-8"
        )
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
        "versions": versions_of(data),
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
    # Ctrl+C in a terminal reaches every process in its group; the analysis must
    # be stopped by the server (which puts the job back in the queue), not by that.
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
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
    """Runs queued analysis jobs on background threads, each log in its own process.

    * Up to ``workers`` jobs run at once; two jobs for the same flight never do.
    * ``start()`` first picks up jobs that a stopped server left unfinished.
    * ``stop(grace_s)`` takes no new jobs, lets running ones finish for up to
      ``grace_s`` seconds, then stops them and puts them back in the queue.
      Queued jobs stay queued. The next ``start()`` runs them all.
    """

    def __init__(
        self,
        store: Store,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        workers: int = DEFAULT_WORKERS,
        max_queue: int = DEFAULT_MAX_QUEUE,
        metrics: Metrics | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.store = store
        self.timeout_s = timeout_s
        self.workers = workers
        self.max_queue = max_queue
        self.metrics = metrics or Metrics()
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._ctx = mp.get_context("spawn")
        self._target: Any = _child  # what the analysis process runs (tests swap it)
        self._lock = threading.Lock()
        self._busy: dict[str, str] = {}  # flight id -> the job running for it
        self._deferred: dict[str, list[str]] = {}  # flight id -> jobs waiting for it
        self._procs: dict[str, Any] = {}  # job id -> its analysis process
        self._interrupted: set[str] = set()
        self._n_interrupted = 0  # analyses stopped by shutdown and put back in the queue
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- lifecycle -------------------------------------------------------------------

    def start(self) -> int:
        """Start the workers; returns how many unfinished jobs were resumed."""
        resumed, given_up = self.store.resume_jobs(MAX_ATTEMPTS)
        for item in resumed:
            self._queue.put(item)
        if resumed or given_up:
            log.info(
                "resuming unfinished jobs", extra={"resumed": len(resumed), "given_up": given_up}
            )
        self._threads = [
            threading.Thread(target=self._loop, name=f"flightlens-jobs-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for t in self._threads:
            t.start()
        return len(resumed)

    def stop(self, grace_s: float = 30) -> dict[str, int]:
        """Stop taking jobs, wait up to ``grace_s`` for running ones, then interrupt them."""
        self._stopping.set()
        for _ in self._threads:
            self._queue.put(None)
        deadline = time.monotonic() + grace_s
        for t in self._threads:
            t.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            still = list(self._procs.items())
            self._interrupted.update(job_id for job_id, _ in still)
        for _, proc in still:
            proc.kill()
        for t in self._threads:
            t.join(10)
        with self._lock:
            interrupted = self._n_interrupted
        left = 0
        while True:
            try:
                left += self._queue.get_nowait() is not None
            except queue.Empty:
                break
        with self._lock:
            left += sum(len(v) for v in self._deferred.values())
        return {"interrupted": interrupted, "left_queued": left}

    # --- state -----------------------------------------------------------------------

    def submit(self, job_id: str, flight_id: str) -> None:
        self._queue.put((job_id, flight_id))

    def pending(self) -> int:
        """Jobs waiting to run."""
        with self._lock:
            deferred = sum(len(v) for v in self._deferred.values())
        with self._queue.mutex:  # the shutdown sentinels (None) are not jobs
            queued = sum(item is not None for item in self._queue.queue)
        return queued + deferred

    def running(self) -> int:
        with self._lock:
            return len(self._busy)

    def full(self) -> bool:
        return self.pending() >= self.max_queue

    def alive(self) -> bool:
        return (
            not self._stopping.is_set()
            and bool(self._threads)
            and all(t.is_alive() for t in self._threads)
        )

    # --- work ------------------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if self._stopping.is_set():
                self._queue.put(item)  # stays queued in the database; resumed at the next start
                return
            job_id, flight_id = item
            with self._lock:
                if flight_id in self._busy:  # never two analyses of one flight at once
                    self._deferred.setdefault(flight_id, []).append(job_id)
                    continue
                self._busy[flight_id] = job_id
            try:
                self._run(job_id, flight_id)
            except Exception as exc:  # never let a worker die
                log.exception("job runner error", extra={"job": job_id, "flight": flight_id})
                self.store.set_job(job_id, "failed", f"internal error: {exc}")
                self.metrics.inc("flightlens_jobs_total", {"outcome": "failed"})
            finally:
                with self._lock:
                    del self._busy[flight_id]
                    waiting = self._deferred.get(flight_id)
                    following = waiting.pop(0) if waiting else None
                    if waiting is not None and not waiting:
                        del self._deferred[flight_id]
                if following is not None:
                    self._queue.put((following, flight_id))

    def _run(self, job_id: str, flight_id: str) -> None:
        ids = {"job": job_id, "flight": flight_id}
        if self._stopping.is_set():
            return  # taken just as the server began to stop: stays queued, resumes next start
        original = self.store.original_path(flight_id)
        if original is None:
            self.store.set_job(job_id, "failed", "The original log file is missing")
            self.metrics.inc("flightlens_jobs_total", {"outcome": "failed"})
            log.warning("job failed: original missing", extra=ids)
            return
        self.store.set_job(job_id, "processing")
        log.info("job started", extra=ids)
        started = time.monotonic()
        parent, child = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(
            target=self._target,
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
        with self._lock:
            self._procs[job_id] = proc
            # started after stop() looked for running analyses: stop it here
            late = self._stopping.is_set() and job_id not in self._interrupted
            if late:
                self._interrupted.add(job_id)
        if late:
            proc.kill()
        status, payload = "crash", "The analysis was stopped"
        try:
            status, payload = self._wait(proc, parent)
            proc.join(timeout=10)
        finally:
            parent.close()
            with self._lock:
                self._procs.pop(job_id, None)
                # also a child that died during shutdown (e.g. from the terminal's Ctrl+C);
                # a real outcome (unreadable log, timeout) stays what it is
                interrupted = job_id in self._interrupted or (
                    self._stopping.is_set() and status == "crash"
                )
                self._interrupted.discard(job_id)
        seconds = round(time.monotonic() - started, 3)

        if status == "ok":
            self.store.record_result(flight_id, payload)
            self.store.set_job(job_id, "done")
            outcome = "done"
            log.info(
                "job done",
                extra={**ids, "seconds": seconds, "verdict": payload.get("verdict")},
            )
        elif interrupted:
            self.store.set_job(job_id, "queued")  # resumed at the next start
            outcome = "interrupted"
            with self._lock:
                self._n_interrupted += 1
            log.warning("job interrupted by shutdown; will resume", extra={**ids})
        elif status == "timeout":
            message = f"Analysis took longer than {self.timeout_s:.0f} s and was stopped"
            self.store.set_job(job_id, "failed", message)
            outcome = "timeout"
            log.warning("job timed out", extra={**ids, "seconds": seconds})
        elif status == "rejected":
            self.store.set_job(job_id, "failed", f"Cannot read this log: {payload}")
            outcome = "rejected"
            log.info("job rejected: unreadable log", extra={**ids, "reason": str(payload)[:200]})
        else:
            first = (str(payload).splitlines() or ["unknown"])[0]
            self.store.set_job(job_id, "failed", f"Analysis error: {first}")
            outcome = "failed"
            log.warning("job failed", extra={**ids, "error": first[:200]})
        self.metrics.inc("flightlens_jobs_total", {"outcome": outcome})
        if outcome != "interrupted":
            self.metrics.observe("flightlens_job_duration_seconds", seconds, buckets=JOB_BUCKETS)

    def _wait(self, proc: Any, parent: Any) -> tuple[str, Any]:
        """The child's answer; checks in short slices so a killed child is noticed at once."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            if parent.poll(0.2):
                try:
                    result: tuple[str, Any] = parent.recv()
                except EOFError:
                    return "crash", "The analysis process exited unexpectedly"
                return result
            if not proc.is_alive():
                if parent.poll(0):
                    continue  # it answered just before exiting
                return "crash", "The analysis process exited unexpectedly"
            if time.monotonic() > deadline:
                proc.kill()
                return "timeout", None
