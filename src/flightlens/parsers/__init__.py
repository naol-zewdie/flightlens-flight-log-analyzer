"""Log format detection and loading.

Each format has its own adapter module that returns a :class:`Flight`:
``ulog`` (PX4 .ulg), ``ardupilot`` (ArduPilot DataFlash .bin) and ``tlog``
(MAVLink telemetry .tlog, from either firmware).
"""

from __future__ import annotations

import dataclasses
import hashlib
from enum import StrEnum
from pathlib import Path

from flightlens.model import Flight

ULOG_MAGIC = b"ULog\x01\x12\x35"
DATAFLASH_HEAD = b"\xa3\x95"
MAVLINK_STX = (0xFE, 0xFD)  # MAVLink v1 and v2 start bytes


class LogFormat(StrEnum):
    DATAFLASH = "dataflash"
    ULOG = "ulog"
    TLOG = "tlog"


class ParseError(Exception):
    """The log could not be read."""


class UnsupportedFormatError(ParseError):
    """The file is not a supported log format."""


def detect_format(path: Path) -> LogFormat:
    """Detect the format from the file contents, not just the extension."""
    with path.open("rb") as fh:
        head = fh.read(16)
    if head.startswith(ULOG_MAGIC):
        return LogFormat.ULOG
    if head.startswith(DATAFLASH_HEAD):
        return LogFormat.DATAFLASH
    # .tlog: each MAVLink packet is prefixed by an 8-byte timestamp.
    if path.suffix.lower() == ".tlog" and len(head) > 8 and head[8] in MAVLINK_STX:
        return LogFormat.TLOG
    raise UnsupportedFormatError(
        f"{path.name} is not an ArduPilot .bin, PX4 .ulg or MAVLink .tlog file"
    )


def file_sha256(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def load(path: Path) -> Flight:
    """Parse a log file into the common data model."""
    fmt = detect_format(path)
    if fmt is LogFormat.ULOG:
        from flightlens.parsers import ulog

        try:
            flight = ulog.parse(path)
        except ulog.ULogParseError as exc:
            raise ParseError(str(exc)) from exc
    elif fmt is LogFormat.DATAFLASH:
        from flightlens.parsers import ardupilot

        try:
            flight = ardupilot.parse(path)
        except ardupilot.DataFlashParseError as exc:
            raise ParseError(str(exc)) from exc
    else:
        from flightlens.parsers import tlog

        try:
            flight = tlog.parse(path)
        except tlog.TlogParseError as exc:
            raise ParseError(str(exc)) from exc

    flight.metadata = dataclasses.replace(flight.metadata, sha256=file_sha256(path))
    return flight
