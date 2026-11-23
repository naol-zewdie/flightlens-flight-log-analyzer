"""Running FlightLens as a service: metrics, structured logs and request ids.

Metrics are kept in memory and served at ``/metrics`` in the Prometheus text
format, with no extra dependency. Logs go through the standard ``logging``
module under the ``flightlens`` logger, as readable lines or as one JSON object
per line (``--log-format json``) for a log collector.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
JOB_BUCKETS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)

Labels = tuple[tuple[str, str], ...]


def _labels(labels: Mapping[str, str] | Labels) -> Labels:
    items = labels.items() if isinstance(labels, Mapping) else labels
    return tuple(sorted((k, str(v)) for k, v in items))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(labels: Labels, extra: Labels = ()) -> str:
    items = labels + extra
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in items) + "}"


def _num(value: float) -> str:
    if value == float("inf"):
        return "+Inf"
    return repr(float(value)) if not float(value).is_integer() else str(int(value))


class Metrics:
    """Counters and histograms, safe to update from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = {}
        self._hists: dict[str, tuple[tuple[float, ...], dict[Labels, list[float]]]] = {}
        self._help: dict[str, tuple[str, str]] = {}
        self.started = time.time()

    def describe(self, name: str, kind: str, text: str) -> None:
        self._help[name] = (kind, text)

    def inc(self, name: str, labels: Mapping[str, str] | None = None, value: float = 1) -> None:
        key = _labels(labels or {})
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0) + value

    def observe(
        self,
        name: str,
        value: float,
        labels: Mapping[str, str] | None = None,
        buckets: tuple[float, ...] = HTTP_BUCKETS,
    ) -> None:
        key = _labels(labels or {})
        with self._lock:
            bounds, series = self._hists.setdefault(name, (buckets, {}))
            # one count per bucket (not cumulative), then sum and count
            row = series.setdefault(key, [0.0] * (len(bounds) + 2))
            for i, bound in enumerate(bounds):
                if value <= bound:
                    row[i] += 1
                    break
            row[-2] += value
            row[-1] += 1

    def value(self, name: str, labels: Mapping[str, str] | None = None) -> float:
        """A counter's current value (0 if never incremented); for tests and health."""
        with self._lock:
            return self._counters.get(name, {}).get(_labels(labels or {}), 0)

    def render(self, gauges: Iterable[tuple[str, str, Iterable[tuple[Labels, float]]]] = ()) -> str:
        """The Prometheus text exposition format (version 0.0.4)."""
        out: list[str] = []

        def head(name: str, kind: str, text: str) -> None:
            out.append(f"# HELP {name} {text}")
            out.append(f"# TYPE {name} {kind}")

        for name, text, series in gauges:
            head(name, "gauge", text)
            for labels, value in series:
                out.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
        with self._lock:
            for name in sorted(self._counters):
                head(name, "counter", self._help.get(name, ("", name))[1])
                for labels, value in sorted(self._counters[name].items()):
                    out.append(f"{name}{_fmt_labels(labels)} {_num(value)}")
            for name in sorted(self._hists):
                bounds, rows = self._hists[name]
                head(name, "histogram", self._help.get(name, ("", name))[1])
                for labels, row in sorted(rows.items()):
                    running = 0.0
                    for bound, count in zip(bounds, row[:-2], strict=True):
                        running += count
                        le = (("le", _num(bound)),)
                        out.append(f"{name}_bucket{_fmt_labels(labels, le)} {_num(running)}")
                    out.append(
                        f"{name}_bucket{_fmt_labels(labels, (('le', '+Inf'),))} {_num(row[-1])}"
                    )
                    out.append(f"{name}_sum{_fmt_labels(labels)} {_num(row[-2])}")
                    out.append(f"{name}_count{_fmt_labels(labels)} {_num(row[-1])}")
        return "\n".join(out) + "\n"


def new_metrics() -> Metrics:
    m = Metrics()
    m.describe("flightlens_http_requests_total", "counter", "HTTP requests by route and status.")
    m.describe(
        "flightlens_http_request_duration_seconds", "histogram", "HTTP request duration by route."
    )
    m.describe("flightlens_uploads_total", "counter", "Uploads by result.")
    m.describe("flightlens_jobs_total", "counter", "Finished analysis jobs by outcome.")
    m.describe("flightlens_job_duration_seconds", "histogram", "Time to analyze one log.")
    return m


# --- logging ---------------------------------------------------------------------------------

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def fields_of(record: logging.LogRecord) -> dict[str, Any]:
    """The structured fields passed with ``extra={...}``."""
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, then the event's fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(fields_of(record))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """``12:04:31 INFO  job done  flight=3f2a… verdict=problem seconds=4.2``"""

    def format(self, record: logging.LogRecord) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(record.created))
        extra = " ".join(f"{k}={_text_value(v)}" for k, v in fields_of(record).items())
        line = f"{when} {record.levelname:<5} {record.getMessage()}"
        if extra:
            line += f"  {extra}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _text_value(value: Any) -> str:
    text = str(value)
    return json.dumps(text, ensure_ascii=False) if (" " in text or not text) else text


LOG_FORMATS = ("text", "json")
LOG_LEVELS = ("debug", "info", "warning", "error")


def setup_logging(fmt: str = "text", level: str = "info", stream: Any = None) -> None:
    """Send FlightLens and uvicorn logs to one handler in the chosen format."""
    if fmt not in LOG_FORMATS:
        raise ValueError(f"log format must be one of {', '.join(LOG_FORMATS)}")
    if level not in LOG_LEVELS:
        raise ValueError(f"log level must be one of {', '.join(LOG_LEVELS)}")
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    for name, lvl in (("flightlens", level), ("uvicorn", "warning"), ("uvicorn.error", "warning")):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(lvl.upper())
        logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True  # requests are logged by FlightLens


# --- request ids -----------------------------------------------------------------------------

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def request_id(incoming: str | None) -> str:
    """Keep a caller's X-Request-ID when it is sensible, otherwise make one."""
    if incoming and _REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex[:16]
