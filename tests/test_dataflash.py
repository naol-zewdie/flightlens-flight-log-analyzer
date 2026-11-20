"""Low-level DataFlash reader tests on generated files."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from flightlens.parsers import dataflash
from flightlens.parsers.dataflash import DataFlashParseError
from flightlens.sim.dfwriter import DFWriter


def _all_types(tmp_path: Path) -> Path:
    w = DFWriter()
    # A format string holds at most 16 characters, so the text types get their own message.
    w.define("TEST", "QbBhHiIfdqcCeELM", "TimeUS,b,B,h,H,i,I,f,d,q,c,C,e,E,L,M")
    # text columns carry the names real firmware uses for text (dataflash.TEXT_COLUMNS)
    w.define("TXT", "QnNZ", "TimeUS,TN,Name,Message")
    for k in range(3):
        w.write(
            "TXT", TimeUS=1_000_000 + k * 1000, TN="abcd", Name="sixteen", Message="a longer text"
        )
        w.write(
            "TEST",
            TimeUS=1_000_000 + k * 1000,
            b=-5,
            B=250,
            h=-30000,
            H=60000,
            i=-2_000_000,
            I=4_000_000_000,
            f=1.5,
            d=2.25,
            q=-(2**40),
            c=-12.34,
            C=56.78,
            e=-1234.56,
            E=7890.12,
            L=-35.1234567,
            M=5,
        )
    return w.save(tmp_path / "types.bin")


def test_every_format_character_round_trips(tmp_path: Path) -> None:
    log = dataflash.read(_all_types(tmp_path))
    c = log.messages["TEST"]
    assert c["TimeUS"].tolist() == [1_000_000, 1_001_000, 1_002_000]
    assert c["b"][0] == -5 and c["B"][0] == 250 and c["h"][0] == -30000 and c["H"][0] == 60000
    assert c["i"][0] == -2_000_000 and c["I"][0] == 4_000_000_000 and c["q"][0] == -(2**40)
    assert c["f"][0] == 1.5 and c["d"][0] == 2.25 and c["M"][0] == 5
    assert c["c"][0] == pytest.approx(-12.34)
    assert c["C"][0] == pytest.approx(56.78)
    assert c["e"][0] == pytest.approx(-1234.56)
    assert c["E"][0] == pytest.approx(7890.12)
    assert c["L"][0] == pytest.approx(-35.1234567, abs=1e-7)
    assert log.text("TXT", "TN") == ["abcd"] * 3
    assert log.text("TXT", "Name") == ["sixteen"] * 3
    assert log.text("TXT", "Message")[0] == "a longer text"
    assert log.start_us == 1_000_000 and log.end_us == 1_002_000
    assert log.issues == []


@pytest.mark.parametrize(
    ("fmt", "cols", "values"),
    [
        ("QZ", "TimeUS,GyrX", {"GyrX": "oops"}),  # a number column declared as text
        ("Qf", "TimeUS,Message", {"Message": 1.0}),  # a text column declared as a number
        ("Qf", "Time,X", {"Time": 1.0, "X": 2.0}),  # no timestamp column
    ],
)
def test_damaged_definitions_are_skipped_not_trusted(
    tmp_path: Path, fmt: str, cols: str, values: dict[str, object]
) -> None:
    w = DFWriter()
    w.define("GOOD", "Qf", "TimeUS,X")
    w.define("BAD", fmt, cols)
    w.write("GOOD", TimeUS=1, X=1.0)
    w.write("BAD", **({"TimeUS": 2} if cols.startswith("TimeUS") else {}), **values)
    log = dataflash.read(w.save(tmp_path / "d.bin"))
    assert set(log.messages) == {"GOOD"}
    assert len(log.issues) == 1 and "BAD" in log.issues[0].message


def test_only_wanted_types_are_decoded(tmp_path: Path) -> None:
    w = DFWriter()
    w.define("AAA", "Qf", "TimeUS,X")
    w.define("BBB", "Qf", "TimeUS,Y")
    w.write("AAA", TimeUS=1, X=1.0)
    w.write("BBB", TimeUS=2, Y=2.0)
    log = dataflash.read(w.save(tmp_path / "w.bin"), wanted={"BBB"})
    assert set(log.messages) == {"BBB"}
    assert log.counts == {"AAA": 1, "BBB": 1}


def test_instances_are_split(tmp_path: Path) -> None:
    w = DFWriter()
    w.define("VIBE", "QBfff", "TimeUS,IMU,VibeX,VibeY,VibeZ")
    for k in range(4):
        w.write("VIBE", TimeUS=1000 + k, IMU=k % 2, VibeZ=float(k))
    log = dataflash.read(w.save(tmp_path / "v.bin"))
    inst = log.instances("VIBE")
    assert sorted(inst) == [0, 1]
    assert inst[1]["VibeZ"].tolist() == [1.0, 3.0]


def test_garbage_in_the_middle_is_skipped_and_reported(tmp_path: Path) -> None:
    w = DFWriter()
    w.define("AAA", "Qf", "TimeUS,X")
    w.write("AAA", TimeUS=1, X=1.0)
    w.raw(bytes(range(1, 101)))  # 100 bytes of damage, no header inside
    w.write("AAA", TimeUS=2, X=2.0)
    log = dataflash.read(w.save(tmp_path / "g.bin"))
    assert log.messages["AAA"]["X"].tolist() == [1.0, 2.0]
    assert log.bytes_skipped == 100
    assert [i.kind for i in log.issues] == ["corrupt"]


def test_truncated_tail_is_dropped(tmp_path: Path) -> None:
    w = DFWriter()
    w.define("AAA", "Qf", "TimeUS,X")
    w.write("AAA", TimeUS=1, X=1.0)
    w.write("AAA", TimeUS=2, X=2.0)
    path = w.save(tmp_path / "t.bin")
    path.write_bytes(path.read_bytes()[:-5])
    log = dataflash.read(path)
    assert log.messages["AAA"]["X"].tolist() == [1.0]
    assert any(i.kind == "truncated" for i in log.issues)


def test_message_without_fmt_is_skipped(tmp_path: Path) -> None:
    w = DFWriter()
    w.define("AAA", "Qf", "TimeUS,X")
    w.raw(b"\xa3\x95\xc8" + b"\x00" * 10)  # type 200 was never defined
    w.write("AAA", TimeUS=1, X=1.0)
    log = dataflash.read(w.save(tmp_path / "u.bin"))
    assert log.messages["AAA"]["X"].tolist() == [1.0]
    assert log.bytes_skipped > 0


def test_old_logs_with_timems(tmp_path: Path) -> None:
    w = DFWriter()
    w.define("ATT", "Iff", "TimeMS,Roll,Pitch")
    w.write("ATT", TimeMS=1500, Roll=1.0)
    log = dataflash.read(w.save(tmp_path / "o.bin"))
    assert log.messages["ATT"]["TimeUS"].tolist() == [1_500_000]


@pytest.mark.parametrize("content", [b"", b"\xa3\x95", b"\xa3\x95\x80" + b"\x00" * 10])
def test_unreadable_files_raise(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.bin"
    path.write_bytes(content)
    with pytest.raises(DataFlashParseError):
        dataflash.read(path)


def test_large_log_reads_quickly(tmp_path: Path) -> None:
    """About 10 MB / 300k messages; the full-size check is in test_ardupilot.py."""
    import time

    w = DFWriter()
    w.define("IMU", "QBffffff", "TimeUS,I,GyrX,GyrY,GyrZ,AccX,AccY,AccZ")
    row = w.buf
    w.write("IMU", TimeUS=1)
    one = bytes(row[-36:])
    w.raw(one * 300_000)
    path = w.save(tmp_path / "big.bin")
    t0 = time.perf_counter()
    log = dataflash.read(path)
    elapsed = time.perf_counter() - t0
    assert log.counts["IMU"] == 300_001
    assert np.all(log.messages["IMU"]["TimeUS"] == 1)
    assert elapsed < 10, elapsed
