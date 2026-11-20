"""Low-level reader for ArduPilot DataFlash logs (``.bin``).

The format is self-describing. Every message is::

    0xA3 0x95 <type: u8> <payload>

and FMT messages (type 128) declare, for every other type, its name, total
length, column names and a format string with one character per column. This
module only turns bytes into named numpy columns; what the columns mean is the
job of :mod:`flightlens.parsers.ardupilot`.

Reading is two passes:

1. Walk the file message by message (lengths come from FMT), remembering the
   offset of each message per type. Damaged bytes are skipped by searching for
   the next header, and counted.
2. Decode only the types the caller asked for, all rows at once, by gathering
   their bytes into one array and viewing it as a numpy structured dtype.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from flightlens.model import ParseIssue

HEAD = b"\xa3\x95"
FMT_TYPE = 128
FMT_LENGTH = 89  # 3 header + Type(B) Length(B) Name(n) Format(N) Columns(Z)
TEXT_CHARS = "nNZ"
TEXT_COLUMNS = frozenset(
    # every text column seen in real ArduPilot logs (Copter, Plane 4.x)
    {"Message", "Name", "Label", "FileName", "Data", "TN", "nam", "FWS", "cal", "dst", "payload"}
    | {"MultIds", "UnitIds"}
)

# Format character -> (numpy type, scale applied after decoding, or None).
FORMAT_CHARS: dict[str, tuple[str, float | None]] = {
    "b": ("i1", None),
    "B": ("u1", None),
    "M": ("u1", None),  # flight mode number
    "h": ("<i2", None),
    "H": ("<u2", None),
    "i": ("<i4", None),
    "I": ("<u4", None),
    "f": ("<f4", None),
    "d": ("<f8", None),
    "q": ("<i8", None),
    "Q": ("<u8", None),
    "c": ("<i2", 0.01),
    "C": ("<u2", 0.01),
    "e": ("<i4", 0.01),
    "E": ("<u4", 0.01),
    "L": ("<i4", 1e-7),  # latitude / longitude in degrees
    "n": ("S4", None),
    "N": ("S16", None),
    "Z": ("S64", None),
    "a": ("(32,)<i2", None),
}

# Columns that hold the sensor or core instance, in order of preference.
INSTANCE_COLUMNS = ("I", "Inst", "IMU", "Instance", "C", "Core")

Columns = dict[str, NDArray[Any]]


class DataFlashParseError(Exception):
    """The file is not a readable DataFlash log."""


@dataclass(frozen=True)
class MessageFormat:
    type: int
    name: str
    length: int
    format: str
    columns: tuple[str, ...]

    def dtype(self) -> np.dtype[np.void]:
        names, types = [], []
        seen: set[str] = set()
        for col, ch in zip(self.columns, self.format, strict=False):
            if ch not in FORMAT_CHARS:
                raise DataFlashParseError(f"{self.name}: unknown format character {ch!r}")
            # Duplicate column names happen in a few old formats; keep them apart.
            name = col if col not in seen else f"{col}_{len(seen)}"
            seen.add(name)
            names.append(name)
            types.append(FORMAT_CHARS[ch][0])
        return np.dtype({"names": names, "formats": types})


@dataclass
class DataFlashLog:
    formats: dict[str, MessageFormat]
    counts: dict[str, int]
    """Number of messages of each type in the file (decoded or not)."""
    messages: dict[str, Columns] = field(default_factory=dict)
    """Decoded columns of the requested types; ``TimeUS`` is always in µs."""
    issues: list[ParseIssue] = field(default_factory=list)
    bytes_skipped: int = 0
    start_us: int = 0
    end_us: int = 0

    def has(self, name: str, column: str | None = None) -> bool:
        cols = self.messages.get(name)
        return cols is not None and (column is None or column in cols)

    def instance_column(self, name: str) -> str | None:
        fmt = self.formats.get(name)
        if fmt is None:
            return None
        for col in INSTANCE_COLUMNS:
            if col in fmt.columns and fmt.format[fmt.columns.index(col)] in "bBhH":
                return col
        return None

    def instances(self, name: str) -> dict[int, Columns]:
        """The message split by its instance column (instance 0 only if it has none)."""
        cols = self.messages.get(name)
        if cols is None:
            return {}
        inst_col = self.instance_column(name)
        if inst_col is None:
            return {0: cols}
        inst = cols[inst_col].astype(np.int64)
        return {
            int(i): {k: v[inst == i] for k, v in cols.items()} for i in np.unique(inst).tolist()
        }

    def time_s(self, cols: Columns) -> NDArray[np.float64]:
        """Seconds since the start of the log."""
        return (cols["TimeUS"].astype(np.float64) - self.start_us) / 1e6

    def text(self, name: str, column: str) -> list[str]:
        cols = self.messages.get(name)
        if cols is None or column not in cols or cols[column].dtype.kind != "S":
            return []  # missing, or a damaged FMT declared the text column as a number
        return [decode(b) for b in cols[column].tolist()]


def decode(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def _read_fmt(data: bytes, pos: int) -> MessageFormat | None:
    body = data[pos + 3 : pos + FMT_LENGTH]
    if len(body) < FMT_LENGTH - 3:
        return None
    mtype, length = body[0], body[1]
    name = decode(body[2:6])
    fmt = decode(body[6:22])
    cols = tuple(c for c in decode(body[22:86]).split(",") if c)
    if not name or length < 3:
        return None
    return MessageFormat(mtype, name, length, fmt, cols)


def read(path: Path, wanted: set[str] | None = None) -> DataFlashLog:
    """Read a DataFlash log, decoding only the ``wanted`` message types (None: all)."""
    data = path.read_bytes()
    if len(data) < 3 or not data.startswith(HEAD):
        raise DataFlashParseError("not an ArduPilot DataFlash log (no A3 95 header)")

    # --- pass 1: walk the file --------------------------------------------------
    lengths = [0] * 256
    lengths[FMT_TYPE] = FMT_LENGTH
    by_type: dict[int, MessageFormat] = {}
    offsets: dict[int, array[int]] = {}
    skipped = 0
    damaged_at: list[int] = []
    truncated = False
    n = len(data)
    pos = 0
    find = data.find
    while pos < n:
        if data[pos] == 0xA3 and pos + 2 < n and data[pos + 1] == 0x95:
            mtype = data[pos + 2]
            length = lengths[mtype]
            if length:
                if pos + length > n:
                    truncated = True
                    break
                if mtype == FMT_TYPE:
                    fmt = _read_fmt(data, pos)
                    if fmt is not None and fmt.type != FMT_TYPE:
                        by_type[fmt.type] = fmt
                        lengths[fmt.type] = fmt.length
                else:
                    offs = offsets.get(mtype)
                    if offs is None:
                        offs = offsets[mtype] = array("Q")
                    offs.append(pos)
                pos += length
                continue
        # Not a message we can read here: skip to the next header.
        nxt = find(HEAD, pos + 1)
        if nxt < 0:
            if n - pos > 3:
                skipped += n - pos
                damaged_at.append(pos)
            break
        skipped += nxt - pos
        damaged_at.append(pos)
        pos = nxt

    formats = {f.name: f for f in by_type.values()}
    counts = {by_type[t].name: len(o) for t, o in offsets.items() if t in by_type}
    if not formats or not counts:
        raise DataFlashParseError("no readable messages (missing FMT definitions)")
    log = DataFlashLog(formats=formats, counts=counts, bytes_skipped=skipped)
    if skipped:
        log.issues.append(
            ParseIssue(
                "corrupt",
                f"{skipped:,} damaged byte(s) skipped in {len(damaged_at)} place(s); "
                "messages in those places are lost",
            )
        )
    if truncated:
        log.issues.append(
            ParseIssue("truncated", "The last message is cut off: the file is truncated")
        )

    # --- pass 2: decode the requested types --------------------------------------
    buf = np.frombuffer(data, dtype=np.uint8)
    for mtype, offs in offsets.items():
        fmt = by_type.get(mtype)
        if fmt is None or (wanted is not None and fmt.name not in wanted):
            continue
        try:
            dtype = fmt.dtype()
        except DataFlashParseError as exc:
            log.issues.append(ParseIssue("invalid_values", str(exc)))
            continue
        if dtype.itemsize != fmt.length - 3:
            log.issues.append(
                ParseIssue(
                    "invalid_values",
                    f"{fmt.name}: format {fmt.format!r} does not match its length {fmt.length}",
                )
            )
            continue
        starts = np.frombuffer(offs, dtype=np.uint64).astype(np.int64) + 3
        rows = buf[starts[:, None] + np.arange(dtype.itemsize)]
        table = rows.view(dtype).reshape(-1)
        # A number column declared as text, or a text column declared as a
        # number: the FMT definition is damaged and the values are nonsense
        # (and would break arithmetic or text handling downstream).
        mistyped = [
            c
            for c, ch in zip(fmt.columns, fmt.format, strict=False)
            if (ch in TEXT_CHARS) != (c in TEXT_COLUMNS)
        ]
        if mistyped:
            log.issues.append(
                ParseIssue(
                    "invalid_values",
                    f"{fmt.name}: damaged format (column {mistyped[0]} has the wrong type); "
                    "ignored",
                )
            )
            continue
        cols = _columns(fmt, table)
        if "TimeUS" not in cols:
            # Every decoded type is placed in time; without a timestamp column
            # (a damaged FMT definition) its rows cannot be used.
            log.issues.append(
                ParseIssue("invalid_values", f"{fmt.name}: no timestamp column; ignored")
            )
            continue
        log.messages[fmt.name] = cols

    _set_time_range(log)
    return log


def _columns(fmt: MessageFormat, table: NDArray[np.void]) -> Columns:
    names = table.dtype.names or ()
    cols: Columns = {}
    for name, ch in zip(names, fmt.format, strict=False):
        values = table[name]
        if ch == "a":  # int16[32]: kept as an (N, 32) array (batch sampler ISBD)
            cols[name] = values.astype(np.float64)
            continue
        scale = FORMAT_CHARS[ch][1]
        cols[name] = values * scale if scale is not None else values.copy()
    if "TimeUS" not in cols and "TimeMS" in cols:  # firmware before about 2015
        cols["TimeUS"] = cols["TimeMS"].astype(np.uint64) * 1000
    return cols


def _set_time_range(log: DataFlashLog) -> None:
    starts, ends = [], []
    for cols in log.messages.values():
        t = cols.get("TimeUS")
        if t is not None and t.size:
            # Ignore zero timestamps (written before the clock started).
            nz = t[t > 0]
            if nz.size:
                starts.append(int(nz.min()))
                ends.append(int(nz.max()))
    if starts:
        log.start_us, log.end_us = min(starts), max(ends)
