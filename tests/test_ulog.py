"""PX4 ULog adapter tests on committed sample logs (see testdata/logs/SOURCES.md)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from flightlens import parsers
from flightlens.engine import Verdict, analyze
from flightlens.parsers.ulog import _quat_to_euler, load_mapping

LOGS = Path(__file__).parent.parent / "testdata" / "logs"
V113 = LOGS / "px4-v1.13-sitl-tagged.ulg"
V16 = LOGS / "px4-v1.6-sample-appended.ulg"


def test_parses_v113_metadata_and_signals() -> None:
    flight = parsers.load(V113)
    md = flight.metadata
    assert md.log_format == "ulog"
    assert md.firmware is not None and md.firmware.startswith("PX4 v1.13.0")
    assert md.vehicle_type == "Quadrotor"
    assert md.sha256 == hashlib.sha256(V113.read_bytes()).hexdigest()
    assert md.mapping_version == load_mapping()["version"]
    for name in ("att.roll", "att.des_roll", "rate.roll", "vibe.metric", "bat.volt", "mot.1"):
        assert name in flight.signals, name
    for sig in flight.signals.values():
        assert sig.source, sig.name
        assert np.all(np.diff(sig.t) >= 0), f"{sig.name} time not sorted"
        assert sig.t.min() >= 0


def test_never_armed_log_is_insufficient_data_not_healthy() -> None:
    flight = parsers.load(V113)
    assert flight.never_armed
    assert any(q.kind == "never_armed" for q in flight.quality)
    assert analyze(flight).verdict is Verdict.INSUFFICIENT_DATA


def test_old_firmware_reports_missing_topics() -> None:
    flight = parsers.load(V16)
    kinds = {q.message for q in flight.quality if q.kind == "missing_topic"}
    assert any("battery" in m for m in kinds)
    assert any("GPS" in m for m in kinds)


def test_truncated_log_still_parses(tmp_path: Path) -> None:
    data = V113.read_bytes()
    cut = tmp_path / "cut.ulg"
    cut.write_bytes(data[: len(data) * 6 // 10])
    flight = parsers.load(cut)
    assert flight.signals
    assert flight.metadata.log_duration is not None


@pytest.mark.parametrize("size", [8, 200])
def test_stub_files_never_look_healthy(tmp_path: Path, size: int) -> None:
    stub = tmp_path / "stub.ulg"
    stub.write_bytes(V113.read_bytes()[:size])
    try:
        flight = parsers.load(stub)
    except parsers.ParseError:
        return  # rejected outright is fine
    assert analyze(flight).verdict is Verdict.INSUFFICIENT_DATA


def test_garbage_after_header_is_rejected_or_degraded(tmp_path: Path) -> None:
    bad = tmp_path / "bad.ulg"
    rng = np.random.default_rng(0)
    bad.write_bytes(V16.read_bytes()[:4000] + rng.bytes(50_000))
    try:
        flight = parsers.load(bad)
    except parsers.ParseError:
        return
    assert analyze(flight).verdict is not Verdict.HEALTHY or flight.quality


@pytest.mark.parametrize(
    ("q", "rpy"),
    [
        ((1, 0, 0, 0), (0, 0, 0)),
        ((np.cos(np.pi / 8), np.sin(np.pi / 8), 0, 0), (45, 0, 0)),
        ((np.cos(np.pi / 12), 0, np.sin(np.pi / 12), 0), (0, 30, 0)),
        ((np.cos(np.pi / 4), 0, 0, np.sin(np.pi / 4)), (0, 0, 90)),
    ],
)
def test_quaternion_to_euler(q: tuple[float, ...], rpy: tuple[float, ...]) -> None:
    arrs = [np.array([v], dtype=np.float64) for v in q]
    out = [float(a[0]) for a in _quat_to_euler(*arrs)]
    assert out == pytest.approx(rpy, abs=1e-9)


def test_mapping_file_is_well_formed() -> None:
    mapping = load_mapping()
    names = [s["name"] for s in mapping["signals"]]
    assert len(names) == len(set(names)), "duplicate signal names"
    for spec in mapping["signals"]:
        assert spec["sources"], spec["name"]
        for src in spec["sources"]:
            assert {"topic", "field"} <= set(src), spec["name"]
