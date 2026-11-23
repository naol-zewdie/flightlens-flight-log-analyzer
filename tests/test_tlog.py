"""MAVLink telemetry logs (.tlog): decoder, adapter, simulator and end to end.

pymavlink (the reference implementation, a development dependency only) checks
the message definitions and both directions of encoding.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pymavlink.dialects.v10 import ardupilotmega as ref1
from pymavlink.dialects.v20 import ardupilotmega as ref2

from flightlens import parsers
from flightlens.api import create_app
from flightlens.engine import Verdict, analyze
from flightlens.model import EventKind, Severity
from flightlens.parsers import mavlink
from flightlens.sim.benchmark import Fault, copter
from flightlens.sim.telemetry import GCS, TlogWriter, telemetry

T0 = 1_777_885_800_000_000  # µs since 1970, a 2026 flight


# --- definitions and framing -----------------------------------------------------------


@pytest.mark.parametrize("msg", list(mavlink.MESSAGES.values()), ids=lambda m: m.name)
def test_definitions_match_the_reference(msg: mavlink.MessageDef) -> None:
    ref = getattr(ref2, f"MAVLink_{msg.name.lower()}_message")
    assert ref.id == msg.id
    assert ref.crc_extra == msg.crc_extra
    assert [f.name for f in msg.fields] == list(ref.ordered_fieldnames)
    assert msg.struct.size == ref.unpacker.size


def _tlog(frames: list[bytes], path: Path) -> Path:
    path.write_bytes(
        b"".join((T0 + i * 100_000).to_bytes(8, "big") + f for i, f in enumerate(frames))
    )
    return path


def _ref_frames(dialect: object) -> list[bytes]:
    mav = dialect.MAVLink(None, srcSystem=1, srcComponent=1)  # type: ignore[attr-defined]
    msgs = [
        mav.heartbeat_encode(2, 3, 128 | 1, 5, 4, 3),
        mav.attitude_encode(1000, 0.1, -0.2, 1.5, 0.01, 0.02, -0.03),
        mav.global_position_int_encode(
            2000, -353632000, 1491652000, 584000, 15000, 120, -50, -30, 9000
        ),
        mav.vibration_encode(3, 14.5, 15.0, 30.25, 1, 2, 3),
        mav.statustext_encode(4, b"PreArm: Gyros inconsistent"),
        mav.param_value_encode(b"MOT_PWM_MIN", 1000.0, 9, 900, 12),
        mav.servo_output_raw_encode(4000, 0, *range(1100, 1108)),
    ]  # fmt: skip
    return [m.pack(mav) for m in msgs]


@pytest.mark.parametrize("dialect", [ref1, ref2], ids=["v1", "v2"])
def test_decodes_reference_frames(dialect: object, tmp_path: Path) -> None:
    data = mavlink.read(_tlog(_ref_frames(dialect), tmp_path / "r.tlog"))
    assert data.packets == 7 and data.crc_errors == 0 and data.unknown == 0
    _, att = data.columns(1, 1, "ATTITUDE")
    assert att["roll"][0] == pytest.approx(0.1) and att["yawspeed"][0] == pytest.approx(-0.03)
    _, pos = data.columns(1, 1, "GLOBAL_POSITION_INT")
    assert pos["lat"][0] == -353632000 and pos["vz"][0] == -30
    _, vib = data.columns(1, 1, "VIBRATION")
    assert vib["vibration_z"][0] == pytest.approx(30.25) and vib["clipping_2"][0] == 3
    _, txt = data.columns(1, 1, "STATUSTEXT")
    assert txt["text"][0].rstrip(b"\0") == b"PreArm: Gyros inconsistent"
    _, srv = data.columns(1, 1, "SERVO_OUTPUT_RAW")
    assert [srv[f"servo{i}_raw"][0] for i in range(1, 9)] == list(range(1100, 1108))
    if dialect is ref2:  # v2 extensions read as zero when absent
        assert srv["servo16_raw"][0] == 0


def test_reads_signed_frames(tmp_path: Path) -> None:
    mav = ref2.MAVLink(None, srcSystem=1, srcComponent=1)
    mav.signing.secret_key = bytes(range(32))
    mav.signing.sign_outgoing = True
    mav.signing.link_id = 0
    mav.signing.timestamp = 1
    frame = mav.attitude_encode(1, 0.5, 0, 0, 0, 0, 0).pack(mav)
    assert frame[2] & 0x01  # the signature flag
    data = mavlink.read(_tlog([frame], tmp_path / "s.tlog"))
    assert data.columns(1, 1, "ATTITUDE")[1]["roll"][0] == pytest.approx(0.5)


@pytest.mark.parametrize("v2", [True, False])
def test_reference_reads_what_we_write(v2: bool) -> None:
    w = TlogWriter(v2=v2)
    frame = w.frame((1, 1), "BATTERY_STATUS", {
        "id": 0, "voltages": [4100, 4090, 4080, 4070] + [65535] * 6, "current_battery": 2150,
        "current_consumed": 960, "battery_remaining": 81, "voltages_ext": [0, 0, 0, 0],
    })  # fmt: skip
    mav = (ref2 if v2 else ref1).MAVLink(None)
    (msg,) = mav.parse_buffer(frame)
    assert msg.get_type() == "BATTERY_STATUS"
    assert list(msg.voltages[:4]) == [4100, 4090, 4080, 4070]
    assert msg.current_consumed == 960 and msg.battery_remaining == 81


def test_v2_truncated_payload_is_restored(tmp_path: Path) -> None:
    w = TlogWriter(v2=True)
    frame = w.frame((1, 1), "SYS_STATUS", {"voltage_battery": 16000})  # mostly zeros
    assert frame[1] < mavlink.BY_NAME["SYS_STATUS"].length
    data = mavlink.read(_tlog([frame], tmp_path / "t.tlog"))
    _, cols = data.columns(1, 1, "SYS_STATUS")
    assert cols["voltage_battery"][0] == 16000 and cols["current_battery"][0] == 0


def test_damage_is_skipped_and_counted(tmp_path: Path) -> None:
    frames = _ref_frames(ref2)
    raw = bytearray(_tlog(frames, tmp_path / "d.tlog").read_bytes())
    raw[40] ^= 0xFF  # inside the second record
    raw[60:60] = b"\x00junk\xfd\xfe"  # inserted garbage
    (tmp_path / "d.tlog").write_bytes(bytes(raw))
    data = mavlink.read(tmp_path / "d.tlog")
    assert 5 <= data.packets < 7 and (data.crc_errors or data.bytes_skipped)
    assert data.columns(1, 1, "VIBRATION")[0].size == 1


def test_sequence_gaps_are_radio_loss(tmp_path: Path) -> None:
    w = TlogWriter()
    for i in range(100):
        frame = w.frame((1, 1), "ATTITUDE", {"time_boot_ms": i})
        if i % 10 != 5:
            w.buf += (T0 + i * 100_000).to_bytes(8, "big") + frame
    data = mavlink.read(w.save(tmp_path / "l.tlog"))
    assert data.received[(1, 1)] == 90 and data.lost[(1, 1)] == 10


def test_no_packets_or_no_vehicle_is_refused(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tlog"
    junk.write_bytes(T0.to_bytes(8, "big") + b"\xfe" + bytes(200))
    with pytest.raises(parsers.ParseError):
        parsers.load(junk)
    w = TlogWriter()
    for i in range(5):
        w.write(T0 + i * 1_000_000, GCS, "HEARTBEAT", type=6, autopilot=8, mavlink_version=3)
    with pytest.raises(parsers.ParseError, match="no vehicle"):
        parsers.load(w.save(tmp_path / "gcs.tlog"))


# --- adapter, from simulated flights --------------------------------------------------------


@pytest.fixture(scope="module")
def flights(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("tlog")
    out = {}
    cases = {
        "healthy": None,
        "vibration": Fault("vibration", 60, 90, 75),
        "gps_loss": Fault("gps_loss", 70, 85),
        "radio_failsafe": Fault("radio_failsafe", 90, 96),
        "arming_refused": Fault("arming_refused"),
    }
    for name, fault in cases.items():
        onboard = parsers.load(copter(d / f"{name}.bin", fault, 7 if fault else 1))
        out[name] = telemetry(onboard, d / f"{name}.tlog", loss=0.03, seed=1)
    out["v1"] = telemetry(parsers.load(d / "healthy.bin"), d / "v1.tlog", v2=False)
    return out


def test_metadata_and_flight_state(flights: dict[str, Path]) -> None:
    fl = parsers.load(flights["healthy"])
    md = fl.metadata
    assert md.log_format == "tlog" and md.mapping_version.startswith("mavlink-")
    assert md.firmware == "ArduCopter V4.6.2 1ebd4d99" and md.vehicle_type == "Quadrotor"
    assert md.hardware == "CubeOrange" and md.serial == "0040003A3138511133383538"
    assert md.start_time is not None and md.start_time.year == 2026
    assert len(fl.segments) == 1 and fl.segments[0].start == pytest.approx(5, abs=1.1)
    assert [e.text for e in fl.events if e.kind is EventKind.MODE] == ["Stabilize", "Loiter"]
    assert fl.parameters["MOT_PWM_MIN"] == 1000
    assert {"att.roll", "pos.rel_alt", "bat.volt", "vibe.clip", "mot.1", "state.landed",
            "mag.x", "power.5v", "ekf.vel"} <= set(fl.signals)  # fmt: skip
    kinds = {q.kind for q in fl.quality}
    assert {"dropout", "low_rate"} <= kinds  # 3% packet loss, and telemetry rates


def test_healthy_telemetry_is_healthy(flights: dict[str, Path]) -> None:
    for name in ("healthy", "v1"):
        report = analyze(parsers.load(flights[name]))
        assert report.verdict is Verdict.HEALTHY, name
        status = {r.check_id: r.status for r in report.results}
        # high-rate analyses say they cannot run, rather than pass
        assert status["vibration.spectrum"] is Severity.NOT_EVALUATED
        assert status["control.tuning"] is Severity.NOT_EVALUATED


@pytest.mark.parametrize(
    ("name", "check"),
    [
        ("vibration", "vibration.level"),
        ("gps_loss", "gps.quality"),
        ("radio_failsafe", "events.failsafes"),
    ],
)
def test_faults_are_found_through_telemetry(
    flights: dict[str, Path], name: str, check: str
) -> None:
    report = analyze(parsers.load(flights[name]))
    assert report.verdict is Verdict.PROBLEM
    assert check in {f.check_id for f in report.findings if f.severity is Severity.PROBLEM}


def test_arming_refusal_through_telemetry(flights: dict[str, Path]) -> None:
    report = analyze(parsers.load(flights["arming_refused"]))
    assert report.verdict is Verdict.INSUFFICIENT_DATA
    (f,) = [f for f in report.findings if f.check_id == "preflight.arming"]
    assert "Gyros inconsistent" in f.explanation


def test_signals_are_in_real_units(flights: dict[str, Path]) -> None:
    fl = parsers.load(flights["healthy"])
    alt = fl.signal("pos.rel_alt").values
    assert 13 < np.nanmax(alt) < 17  # climbs to 15 m
    volt = fl.signal("bat.volt").values
    assert 15 < np.nanmedian(volt) < 17  # 4S
    assert 0.4 < np.nanmedian(np.abs(fl.signal("mag.z").values)) < 0.6  # gauss


# --- through the web app -----------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path), base_url="http://localhost") as c:
        yield c


def test_upload_a_tlog(client: TestClient, flights: dict[str, Path]) -> None:
    import time

    data = flights["vibration"].read_bytes()
    r = client.post("/api/logs", files={"file": ("flight 2026-05-04.tlog", data)})
    assert r.status_code == 202, r.text
    fid = r.json()["flight_id"]
    for _ in range(120):
        row = client.get(f"/api/flights/{fid}").json()["flight"]
        if row["status"] in ("done", "failed"):
            break
        time.sleep(0.25)
    assert row["status"] == "done" and row["verdict"] == "problem"
    assert row["log_format"] == "tlog"
    assert client.get(f"/api/flights/{fid}").json()["stale"] == []
