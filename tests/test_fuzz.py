"""Damaged and hostile logs: the parsers either read them or refuse them cleanly.

For any input, ``parsers.load`` returns a flight or raises ``ParseError``
(never another exception, never a hang), and no check crashes on what it
returns. CI runs a modest number of Hypothesis examples; set
``FLIGHTLENS_FUZZ=<n>`` for more, or run ``scripts/fuzz.py`` for long campaigns.

``fuzz_regressions/`` holds inputs that once broke a parser, stored as byte
patches against a sample log in the repository.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from functools import cache
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flightlens import parsers
from flightlens.engine import analyze
from flightlens.model import Severity

ROOT = Path(__file__).resolve().parents[1]
REGRESSIONS = sorted((Path(__file__).parent / "fuzz_regressions").glob("*.json"))
EXAMPLES = int(os.environ.get("FLIGHTLENS_FUZZ", "40"))
MAX_SECONDS = 20.0

FUZZ = settings(
    max_examples=EXAMPLES,
    deadline=None,
    derandomize=not os.environ.get("FLIGHTLENS_FUZZ"),  # CI: the same examples every run
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def handled(path: Path) -> str:
    """'ok' or 'refused'; fails the test on anything else."""
    start = time.monotonic()
    try:
        flight = parsers.load(path)
    except parsers.ParseError:
        outcome = "refused"
    else:
        report = analyze(flight)
        crashed = [r.check_id for r in report.results if r.status is Severity.ERROR]
        assert not crashed, f"checks crashed: {crashed}"
        outcome = "ok"
    assert time.monotonic() - start < MAX_SECONDS
    return outcome


@cache
def seed_log(kind: str) -> bytes:
    if kind == "ulog":
        return (ROOT / "testdata/logs/px4-v1.6-sample-appended.ulg").read_bytes()
    if kind == "tlog":
        import tempfile

        from flightlens import parsers
        from flightlens.sim.benchmark import copter
        from flightlens.sim.telemetry import telemetry

        with tempfile.TemporaryDirectory() as tmp:
            flight = parsers.load(copter(Path(tmp) / "c.bin", None, 2))
            return telemetry(flight, Path(tmp) / "c.tlog", loss=0.02).read_bytes()
    import tempfile

    from flightlens.sim.plane import plane_log

    with tempfile.TemporaryDirectory() as tmp:
        return plane_log(Path(tmp) / "p.bin", "crash", trim=-0.2).read_bytes()


@st.composite
def damaged(draw: st.DrawFn, kind: str) -> bytes:
    data = bytearray(seed_log(kind))
    n = len(data)
    how = draw(st.sampled_from(["truncate", "flip", "overwrite", "insert", "zero"]))
    if how == "truncate":
        return bytes(data[: draw(st.integers(0, n))])
    if how == "flip":
        for _ in range(draw(st.integers(1, 16))):
            data[draw(st.integers(0, n - 1))] ^= 1 << draw(st.integers(0, 7))
    elif how == "overwrite":
        at = draw(st.integers(0, n - 1))
        junk = draw(st.binary(min_size=1, max_size=256))
        data[at : at + len(junk)] = junk
    elif how == "insert":
        at = draw(st.integers(0, n))
        data[at:at] = draw(st.binary(min_size=1, max_size=256))
    else:
        at = draw(st.integers(0, n - 1))
        size = draw(st.integers(1, 4096))
        data[at : at + size] = bytes(len(data[at : at + size]))
    return bytes(data)


@pytest.mark.parametrize(
    ("kind", "suffix"), [("ulog", ".ulg"), ("dataflash", ".bin"), ("tlog", ".tlog")]
)
def test_damaged_logs_never_crash(kind: str, suffix: str, tmp_path: Path) -> None:
    @FUZZ
    @given(damaged(kind))
    def run(data: bytes) -> None:
        path = tmp_path / f"case{suffix}"
        path.write_bytes(data)
        handled(path)

    run()


@FUZZ
@given(
    body=st.binary(max_size=2048),
    head=st.sampled_from([b"ULog\x01\x12\x35", b"\xa3\x95", b""]),
)
def test_random_bytes_with_a_valid_header(
    body: bytes, head: bytes, tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("r") / "case.bin"
    path.write_bytes(head + body)
    with contextlib.suppress(parsers.UnsupportedFormatError):  # no header: refused early
        handled(path)


@pytest.mark.parametrize("case", REGRESSIONS, ids=[p.stem for p in REGRESSIONS])
def test_known_bad_inputs(case: Path, tmp_path: Path) -> None:
    spec = json.loads(case.read_text(encoding="utf-8"))
    base = (ROOT / spec["base"]).read_bytes()
    assert hashlib.sha256(base).hexdigest() == spec["base_sha256"], "the base log changed"
    data = bytearray(base)
    for offset, hexbytes in spec["changes"]:
        chunk = bytes.fromhex(hexbytes)
        data[offset : offset + len(chunk)] = chunk
    path = tmp_path / "case.ulg"
    path.write_bytes(bytes(data))
    handled(path)


def test_hang_becomes_a_clear_error(tmp_path: Path) -> None:
    case = next(p for p in REGRESSIONS if p.stem == "ulog-reader-loops-forever")
    test_known_bad_inputs(case, tmp_path)
    with pytest.raises(parsers.ParseError, match="re-reading it without making progress"):
        parsers.load(tmp_path / "case.ulg")
