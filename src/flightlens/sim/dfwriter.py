"""A minimal ArduPilot DataFlash (.bin) writer.

It packs rows with :mod:`struct`, independently of the reader's numpy dtype
code, so reading back what it writes is a genuine cross-check.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

STRUCT: dict[str, tuple[str, float]] = {
    "b": ("b", 1),
    "B": ("B", 1),
    "M": ("B", 1),
    "h": ("h", 1),
    "H": ("H", 1),
    "i": ("i", 1),
    "I": ("I", 1),
    "f": ("f", 1),
    "d": ("d", 1),
    "q": ("q", 1),
    "Q": ("Q", 1),
    "c": ("h", 0.01),
    "C": ("H", 0.01),
    "e": ("i", 0.01),
    "E": ("I", 0.01),
    "L": ("i", 1e-7),
    "n": ("4s", 1),
    "N": ("16s", 1),
    "Z": ("64s", 1),
    "a": ("64s", 1),  # int16[32], written as raw bytes
}


class DFWriter:
    def __init__(self) -> None:
        self.buf = bytearray()
        self.types: dict[str, tuple[int, str, list[str], struct.Struct]] = {}
        self._fmt_record(128, 89, "FMT", "BBnNZ", "Type,Length,Name,Format,Columns")

    def _fmt_record(self, mtype: int, length: int, name: str, fmt: str, cols: str) -> None:
        self.buf += b"\xa3\x95\x80" + struct.pack(
            "<BB4s16s64s", mtype, length, name.encode(), fmt.encode(), cols.encode()
        )

    def define(self, name: str, fmt: str, columns: str) -> None:
        mtype = 129 + len(self.types)
        if name in ("PARM", "MSG", "ERR", "EV", "MODE", "ARM"):
            mtype = {"PARM": 64, "MSG": 91, "ERR": 13, "EV": 10, "MODE": 11, "ARM": 12}[name]
        packer = struct.Struct("<" + "".join(STRUCT[c][0] for c in fmt))
        self.types[name] = (mtype, fmt, columns.split(","), packer)
        self._fmt_record(mtype, packer.size + 3, name, fmt, columns)

    def write(self, name: str, **values: Any) -> None:
        mtype, fmt, cols, packer = self.types[name]
        packed = []
        for col, ch in zip(cols, fmt, strict=True):
            v = values.get(col, 0)
            code, scale = STRUCT[ch]
            if code.endswith("s"):
                v = v.encode() if isinstance(v, str) else (v or b"")
            elif scale != 1:
                v = round(v / scale)
            elif code in "fd":
                v = float(v)
            else:
                v = int(v)
            packed.append(v)
        self.buf += b"\xa3\x95" + bytes([mtype]) + packer.pack(*packed)

    def raw(self, data: bytes) -> None:
        self.buf += data

    def save(self, path: Path) -> Path:
        path.write_bytes(bytes(self.buf))
        return path
