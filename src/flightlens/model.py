"""Common data model.

Every parser (ArduPilot .bin, PX4 .ulg, MAVLink .tlog) produces a :class:`Flight`.
Checks only ever see this model, never format-specific message names.

Conventions:
- Time is in seconds since the start of the log (float64).
- Signal names use the common vocabulary, e.g. ``vibe.z``, ``bat.volt``.
- Units are SI unless the unit string says otherwise.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]


class Severity(StrEnum):
    """Outcome of a check.

    ``NOT_EVALUATED`` means the check could not run (missing or unusable data).
    It is never treated as a pass. ``ERROR`` means the check itself crashed.
    """

    OK = "ok"
    ATTENTION = "attention"
    PROBLEM = "problem"
    NOT_EVALUATED = "not_evaluated"
    ERROR = "error"

    @property
    def rank(self) -> int:
        """Higher is more serious; used to order findings."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.PROBLEM: 4,
    Severity.ATTENTION: 3,
    Severity.ERROR: 2,
    Severity.NOT_EVALUATED: 1,
    Severity.OK: 0,
}


class EventKind(StrEnum):
    MODE = "mode"
    ARM = "arm"
    DISARM = "disarm"
    ERROR = "error"
    FAILSAFE = "failsafe"
    MESSAGE = "message"


@dataclass(frozen=True)
class Signal:
    """One time series in the common vocabulary."""

    name: str
    unit: str
    t: FloatArray
    values: FloatArray
    source: str = ""
    """Original message and field, e.g. ``VIBE.VibeZ``, kept for traceability."""

    def __post_init__(self) -> None:
        if self.t.ndim != 1 or self.t.shape != self.values.shape:
            raise ValueError(
                f"signal {self.name!r}: t and values must be 1-D arrays of equal length "
                f"(got {self.t.shape} and {self.values.shape})"
            )

    def __len__(self) -> int:
        return int(self.t.shape[0])


@dataclass(frozen=True)
class Event:
    t: float
    kind: EventKind
    text: str
    level: str = "info"
    """``info``, ``warning``, ``error`` or ``critical``."""
    source: str = ""


@dataclass(frozen=True)
class Segment:
    """An armed-to-disarmed interval of the log."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class FlightMetadata:
    log_format: str
    source_path: Path | None = None
    sha256: str | None = None
    vehicle_type: str | None = None
    firmware: str | None = None
    hardware: str | None = None
    serial: str | None = None
    """Unique id of the autopilot board (PX4 sys_uuid, ArduPilot board UID); groups
    flights of one aircraft. Board swaps and placeholder ids exist, so it only
    suggests the aircraft; a user's label wins."""
    start_time: datetime | None = None
    log_duration: float | None = None
    """Seconds from the first to the last timestamp in the log."""
    mapping_version: str | None = None
    """Version of the signal mapping table used by the parser."""


@dataclass(frozen=True)
class ParseIssue:
    """Something about the log's data quality that a reader should know."""

    kind: str
    """``dropout``, ``missing_topic``, ``non_monotonic``, ``truncated``, ``invalid_values``..."""
    message: str
    t: float | None = None


@dataclass
class Flight:
    metadata: FlightMetadata
    signals: dict[str, Signal] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    parameters: dict[str, float] = field(default_factory=dict)
    segments: list[Segment] = field(default_factory=list)
    quality: list[ParseIssue] = field(default_factory=list)
    arming_known: bool = False
    """True when the log records arming state. Then an empty ``segments`` list
    means the aircraft never armed, not that arming is unknown."""

    def has(self, *names: str) -> bool:
        return all(n in self.signals for n in names)

    def signal(self, name: str) -> Signal:
        try:
            return self.signals[name]
        except KeyError:
            raise KeyError(f"signal {name!r} is not in this flight") from None

    @property
    def never_armed(self) -> bool:
        return self.arming_known and not self.segments

    def armed_mask(self, t: FloatArray) -> BoolArray:
        """Boolean mask of samples that fall inside an armed segment.

        If the log has no arming information at all, every sample counts as
        armed. If it records arming and the aircraft never armed, no sample does.
        """
        if not self.segments:
            return np.full(t.shape, not self.arming_known, dtype=np.bool_)
        mask = np.zeros(t.shape, dtype=np.bool_)
        for seg in self.segments:
            mask |= (t >= seg.start) & (t <= seg.end)
        return mask

    def segment_ids(self, t: FloatArray) -> NDArray[np.int64]:
        """Index of the armed segment each sample falls in, or -1 if disarmed.

        Use it to avoid differencing across a disarmed gap between two flights
        in one log. Without arming information every sample is segment 0.
        """
        if not self.segments:
            return np.full(t.shape, -1 if self.arming_known else 0, dtype=np.int64)
        ids = np.full(t.shape, -1, dtype=np.int64)
        for i, seg in enumerate(self.segments):
            ids[(t >= seg.start) & (t <= seg.end) & (ids < 0)] = i
        return ids


@dataclass(frozen=True)
class Finding:
    """One result of a check, with the evidence needed to trust it."""

    check_id: str
    severity: Severity
    title: str
    explanation: str
    recommendation: str = ""
    measured: float | None = None
    threshold: float | None = None
    comparison: str = ">"
    """How ``measured`` relates to ``threshold`` when breached: ``>`` or ``<``."""
    unit: str = ""
    start: float | None = None
    end: float | None = None
    evidence_signals: tuple[str, ...] = ()
    related: tuple[str, ...] = ()
    """Other findings this one is linked to, e.g. likely causes of a crash."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "severity": self.severity.value,
            "title": self.title,
            "explanation": self.explanation,
            "recommendation": self.recommendation,
            "measured": self.measured,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "unit": self.unit,
            "start": self.start,
            "end": self.end,
            "evidence_signals": list(self.evidence_signals),
            "related": list(self.related),
        }


CheckConfig = Mapping[str, float]
