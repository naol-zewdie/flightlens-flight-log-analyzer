from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from flightlens import parsers
from flightlens.parsers import LogFormat, UnsupportedFormatError, detect_format


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_detects_ulog_by_magic(tmp_path: Path) -> None:
    p = write(tmp_path, "renamed.bin", parsers.ULOG_MAGIC + b"\x01" + b"\0" * 16)
    assert detect_format(p) is LogFormat.ULOG


def test_detects_dataflash(tmp_path: Path) -> None:
    p = write(tmp_path, "00000042.BIN", b"\xa3\x95\x80" + b"\0" * 16)
    assert detect_format(p) is LogFormat.DATAFLASH


def test_detects_tlog(tmp_path: Path) -> None:
    p = write(tmp_path, "flight.tlog", b"\0" * 8 + b"\xfd" + b"\0" * 16)
    assert detect_format(p) is LogFormat.TLOG


@pytest.mark.parametrize("data", [b"", b"hello world, not a log", b"\0" * 32])
def test_rejects_unknown(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(UnsupportedFormatError):
        detect_format(write(tmp_path, "x.bin", data))


def test_sha256_matches_hashlib(tmp_path: Path) -> None:
    data = b"flightlens" * 300_000
    p = write(tmp_path, "big.bin", data)
    assert parsers.file_sha256(p, chunk_size=4096) == hashlib.sha256(data).hexdigest()
