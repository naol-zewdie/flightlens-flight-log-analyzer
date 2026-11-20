"""ArduPilot .bin adapter tests on generated logs (see flightlens/sim/dfwriter.py).

Each test builds a 120 s Copter flight: log starts at boot + 50 s, arm at
5 s, takeoff at 8 s, Loiter from 20 s, landing at 110 s, disarm at 112 s,
log end at 120 s. Options inject one fault at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from flightlens import parsers
from flightlens.engine import Verdict, analyze
from flightlens.model import EventKind, Severity
from flightlens.sim.dfwriter import DFWriter

BOOT_US = 50_000_000
UTC_START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)


def _gps_time(t: float) -> tuple[int, int]:
    gps = UTC_START + timedelta(seconds=t + 18) - GPS_EPOCH
    week, rem = divmod(gps.total_seconds(), 7 * 86400)
    return int(week), round(rem * 1000)


def copter_log(
    path: Path,
    *,
    use_arm_msg: bool = True,
    arm: bool = True,
    disarm: bool = True,
    vibe_z_fault: bool = False,
    clip_fault: bool = False,
    battery_failsafe: bool = False,
    quadplane: bool = False,
    rest_voltage: float = 16.4,
    duration: float = 120.0,
) -> Path:
    w = DFWriter()
    w.define("PARM", "QNf", "TimeUS,Name,Value")
    w.define("MSG", "QZ", "TimeUS,Message")
    w.define("EV", "QB", "TimeUS,Id")
    if use_arm_msg:
        w.define("ARM", "QBIBB", "TimeUS,ArmState,ArmChecks,Forced,Method")
    w.define("MODE", "QMBB", "TimeUS,Mode,ModeNum,Rsn")
    w.define("ERR", "QBB", "TimeUS,Subsys,ECode")
    w.define("ATT", "QccccCCCC", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,ErrRP,ErrYaw")
    w.define("RATE", "Qfffffffff", "TimeUS,RDes,R,ROut,PDes,P,POut,YDes,Y,YOut")
    w.define("VIBE", "QBfffI", "TimeUS,IMU,VibeX,VibeY,VibeZ,Clip")
    w.define("BAT", "QBfffB", "TimeUS,Inst,Volt,Curr,CurrTot,RemPct")
    w.define("GPS", "QBBIHBcLLeff", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,VZ")
    w.define("POS", "QLLff", "TimeUS,Lat,Lng,Alt,RelHomeAlt")
    w.define("XKF1", "QBfff", "TimeUS,C,VN,VE,VD")
    w.define("XKF4", "QBcccc", "TimeUS,C,SV,SP,SH,SM")
    w.define("RCOU", "Q" + "H" * 14, "TimeUS," + ",".join(f"C{i}" for i in range(1, 15)))
    w.define("CTUN", "Qf", "TimeUS,ThO")

    def us(t: float) -> int:
        return BOOT_US + round(t * 1e6)

    params: dict[str, float] = {"MOT_PWM_MIN": 1000, "MOT_PWM_MAX": 2000}
    for ch in range(1, 5):
        params[f"SERVO{ch}_FUNCTION"] = 32 + ch
    if quadplane:
        params.update(
            {"SERVO5_FUNCTION": 70, "SERVO5_MIN": 1000, "SERVO5_MAX": 2000, "Q_ENABLE": 1}
        )
    for name, value in params.items():
        w.write("PARM", TimeUS=us(0), Name=name, Value=value)
    vehicle = "ArduPlane V4.5.7 (1a2b3c4d)" if quadplane else "ArduCopter V4.5.7 (1a2b3c4d)"
    w.write("MSG", TimeUS=us(0), Message=vehicle)
    w.write("MSG", TimeUS=us(0), Message="CubeOrange 0040003A3138511133383538")
    if not quadplane:
        w.write("MSG", TimeUS=us(0), Message="Frame: QUAD/X")
    w.write("MODE", TimeUS=us(0), Mode=0, ModeNum=0, Rsn=0)

    events = [(5.0, "arm"), (8.0, "takeoff"), (20.0, "loiter"), (110.0, "land"), (112.0, "disarm")]
    if not arm:
        events = [e for e in events if e[1] == "loiter"]
    if not disarm:
        events = [e for e in events if e[1] not in ("land", "disarm")]
    clip = 0
    for k in range(int(duration * 10)):
        t = k / 10
        for at, what in events:
            if abs(t - at) < 1e-9:
                if what == "arm":
                    w.write("EV", TimeUS=us(t), Id=10)
                    if use_arm_msg:
                        w.write("ARM", TimeUS=us(t), ArmState=1)
                elif what == "disarm":
                    w.write("EV", TimeUS=us(t), Id=11)
                    if use_arm_msg:
                        w.write("ARM", TimeUS=us(t), ArmState=0)
                elif what == "takeoff":
                    w.write("EV", TimeUS=us(t), Id=28)
                elif what == "land":
                    w.write("EV", TimeUS=us(t), Id=18)
                elif what == "loiter":
                    w.write("MODE", TimeUS=us(t), Mode=5, ModeNum=5, Rsn=1)
        if battery_failsafe and abs(t - 60.0) < 1e-9:
            w.write("ERR", TimeUS=us(t), Subsys=6, ECode=1)

        flying = arm and 8 <= t <= 110
        armed = arm and 5 <= t <= (112 if disarm else duration)
        roll = 2 * np.sin(t) if flying else 0.0
        w.write(
            "ATT",
            TimeUS=us(t),
            DesRoll=roll,
            Roll=roll + 0.3,
            DesPitch=0,
            Pitch=0.2,
            DesYaw=90,
            Yaw=90,
        )
        w.write("RATE", TimeUS=us(t), RDes=0.5, R=0.4, PDes=0, P=0.1, YDes=0, Y=0)
        vz = 70.0 if vibe_z_fault and 50 <= t < 62 else 12.0
        if clip_fault and 40 <= t < 60:
            clip += 10
        w.write("VIBE", TimeUS=us(t), IMU=0, VibeX=8.0, VibeY=9.0, VibeZ=vz, Clip=clip)
        w.write("VIBE", TimeUS=us(t) + 50, IMU=1, VibeX=5.0, VibeY=5.0, VibeZ=8.0, Clip=0)
        volt = (rest_voltage - 0.6) if flying else rest_voltage
        used = max(0.0, t - 8) * 50
        w.write("BAT", TimeUS=us(t), Inst=0, Volt=volt, Curr=20 if flying else 1, CurrTot=used)
        wk, ms = _gps_time(t)
        w.write(
            "GPS",
            TimeUS=us(t),
            I=0,
            Status=3,
            GMS=ms,
            GWk=wk,
            NSats=14,
            HDop=0.8,
            Lat=-35.3632 + t * 1e-6,
            Lng=149.1652,
            Alt=584.0,
            Spd=1.0,
        )
        alt = 10.0 if flying else 0.0
        w.write(
            "POS",
            TimeUS=us(t),
            Lat=-35.3632 + t * 1e-6,
            Lng=149.1652,
            Alt=584 + alt,
            RelHomeAlt=alt,
        )
        w.write("XKF1", TimeUS=us(t), C=0, VN=0.1, VE=0.0, VD=0.0)
        w.write("XKF4", TimeUS=us(t), C=0, SV=0.1, SP=0.1, SH=0.1, SM=0.1)
        pwm = 1500 if flying else (1100 if armed else 1000)
        w.write("RCOU", TimeUS=us(t), C1=pwm, C2=pwm, C3=pwm, C4=pwm, C5=1600 if flying else 1000)
        w.write("CTUN", TimeUS=us(t), ThO=0.5 if flying else 0.0)
    return w.save(path)


def _load(tmp_path: Path, **kw: Any) -> Any:
    return parsers.load(copter_log(tmp_path / "flight.bin", **kw))


def _finding(report: Any, check_id: str) -> Any:
    return next(f for f in report.findings if f.check_id == check_id)


# --- parsing ------------------------------------------------------------------


def test_healthy_copter_flight(tmp_path: Path) -> None:
    flight = _load(tmp_path)
    md = flight.metadata
    assert md.log_format == "dataflash"
    assert md.firmware == "ArduCopter V4.5.7 1a2b3c4d"
    assert md.vehicle_type == "Quadrotor"
    assert md.hardware == "CubeOrange"
    assert md.start_time == UTC_START
    assert md.log_duration == pytest.approx(119.9, abs=0.01)
    assert md.mapping_version.startswith("ardupilot-")
    assert md.sha256 and len(md.sha256) == 64

    for name in (
        "att.roll", "att.des_roll", "rate.roll", "vibe.x", "vibe.z", "vibe.clip",
        "bat.volt", "bat.cells", "gps.fix", "gps.hdop", "pos.rel_alt", "pos.climb",
        "ekf.hgt", "mot.1", "mot.4", "state.landed", "state.att_ctrl", "ctrl.thrust",
    ):  # fmt: skip
        assert name in flight.signals, name
    for sig in flight.signals.values():
        assert np.all(np.diff(sig.t) >= 0), sig.name
        assert sig.t.min() >= 0, sig.name
    assert "pusher.1" not in flight.signals
    assert flight.signals["mot.1"].values.max() == pytest.approx(0.5)
    assert flight.signals["vibe.z"].source.startswith("VIBE[0]")  # the worse IMU

    assert [(s.start, s.end) for s in flight.segments] == [(5.0, 112.0)]
    modes = [e.text for e in flight.events if e.kind is EventKind.MODE]
    assert modes == ["Stabilize", "Loiter"]

    report = analyze(flight)
    assert report.verdict is Verdict.HEALTHY, [
        (f.check_id, f.severity, f.title) for f in report.findings
    ]


def test_arming_from_ev_only_on_older_logs(tmp_path: Path) -> None:
    flight = _load(tmp_path, use_arm_msg=False)
    assert flight.arming_known
    assert [(s.start, s.end) for s in flight.segments] == [(5.0, 112.0)]


def test_never_armed_is_insufficient_data(tmp_path: Path) -> None:
    flight = _load(tmp_path, arm=False)
    assert flight.never_armed
    assert any(q.kind == "never_armed" for q in flight.quality)
    assert analyze(flight).verdict is Verdict.INSUFFICIENT_DATA


def test_log_ending_armed_is_reported(tmp_path: Path) -> None:
    flight = _load(tmp_path, disarm=False)
    assert any(q.kind == "ends_armed" for q in flight.quality)
    assert flight.segments[-1].end == pytest.approx(119.9, abs=0.01)


def test_cell_count_estimated_from_resting_voltage(tmp_path: Path) -> None:
    flight = _load(tmp_path)
    assert flight.signals["bat.cells"].values[0] == 4
    assert any(q.kind == "estimated" for q in flight.quality)


def test_ambiguous_cell_count_leaves_battery_not_evaluated(tmp_path: Path) -> None:
    flight = _load(tmp_path, rest_voltage=12.4)  # 3S at 4.13 V/cell (4S would be 3.1 V)
    assert flight.signals["bat.cells"].values[0] == 3
    flight = _load(tmp_path, rest_voltage=8.0)  # 2S at 4.0 V/cell only
    assert flight.signals["bat.cells"].values[0] == 2
    flight = _load(tmp_path, rest_voltage=29.0)  # 7S at 4.14 or 8S at 3.63: ambiguous
    assert "bat.cells" not in flight.signals
    assert _finding(analyze(flight), "battery.sag").severity is Severity.NOT_EVALUATED


def test_quadplane_pusher_is_not_a_lift_motor(tmp_path: Path) -> None:
    flight = _load(tmp_path, quadplane=True)
    assert flight.metadata.vehicle_type == "VTOL (QuadPlane)"
    assert "pusher.1" in flight.signals and "mot.5" not in flight.signals
    assert [f"mot.{i}" in flight.signals for i in range(1, 5)] == [True] * 4


# --- faults reach the checks ------------------------------------------------------


def test_vibration_fault_is_a_problem(tmp_path: Path) -> None:
    report = analyze(_load(tmp_path, vibe_z_fault=True))
    f = _finding(report, "vibration.level")
    assert f.severity is Severity.PROBLEM
    assert f.start == pytest.approx(50.0, abs=0.2)
    assert report.verdict is Verdict.PROBLEM


def test_clipping_fault_is_a_problem(tmp_path: Path) -> None:
    f = _finding(analyze(_load(tmp_path, clip_fault=True)), "imu.clipping")
    assert f.severity is Severity.PROBLEM
    assert f.measured == pytest.approx(2000)


def test_battery_failsafe_in_flight_is_a_problem(tmp_path: Path) -> None:
    flight = _load(tmp_path, battery_failsafe=True)
    fs = [e for e in flight.events if e.kind is EventKind.FAILSAFE]
    assert [e.text for e in fs] == ["Battery failsafe"]
    f = _finding(analyze(flight), "events.failsafes")
    assert f.severity is Severity.PROBLEM


def test_cli_exit_code_on_faulty_bin(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = copter_log(tmp_path / "bad.bin", vibe_z_fault=True)
    proc = subprocess.run(
        [sys.executable, "-m", "flightlens", "analyze", str(path)], capture_output=True, text=True
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


@pytest.mark.skipif(
    not __import__("os").environ.get("FLIGHTLENS_SLOW"), reason="set FLIGHTLENS_SLOW=1 to run"
)
def test_100mb_log_parses_within_spec_target(tmp_path: Path) -> None:
    import time

    one = copter_log(tmp_path / "one.bin").read_bytes()
    big = tmp_path / "big.bin"
    big.write_bytes(one * (100_000_000 // len(one) + 1))
    t0 = time.perf_counter()
    parsers.load(big)
    assert time.perf_counter() - t0 < 30
