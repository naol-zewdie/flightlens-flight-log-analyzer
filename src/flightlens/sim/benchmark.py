"""Generated fault benchmark: logs with one known, injected fault each.

``build(out_dir)`` writes every scenario as an ArduPilot ``.bin`` plus a
validation manifest (``benchmark.yaml``) saying what FlightLens must report.
``flightlens validate <out_dir>`` then scores it; ``tests/test_validation.py``
runs it in CI and ``scripts/benchmark.py`` regenerates ``docs/validation.md``.

These logs prove the detection logic against fault *signatures as modelled
here* (with sensor noise, marginal levels and healthy flights near every
limit). They do not prove the signatures are realistic; that is what the SITL
campaign (``scripts/sitl_campaign.py``) and real labelled logs are for.

Copter flight: 4S quad (5200 mAh), arm 5 s, takeoff 8 s, climb to 15 m by 15 s, Loiter
from 20 s, land 165 s, disarm 168 s, log end 175 s, 10 Hz (rate loop 100 Hz).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from flightlens.sim.dfwriter import DFWriter
from flightlens.sim.plane import plane_log

BOOT_US = 40_000_000
UTC_START = datetime(2026, 5, 4, 9, 30, 0, tzinfo=UTC)
GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)
CELLS = 4
LAND, DISARM, END = 165.0, 168.0, 175.0


@dataclass(frozen=True)
class Fault:
    kind: str
    start: float = 60.0
    end: float = 90.0
    level: float = 0.0


def _gps_time(t: float) -> tuple[int, int]:
    gps = UTC_START + timedelta(seconds=t + 18) - GPS_EPOCH
    week, rem = divmod(gps.total_seconds(), 7 * 86400)
    return int(week), round(rem * 1000)


def copter(
    path: Path,
    fault: Fault | None = None,
    seed: int = 0,
    extra: dict[str, float] | None = None,
    day: int = 0,
) -> Path:
    rng = np.random.default_rng(seed)
    kind = fault.kind if fault else "healthy"
    level = fault.level if fault else 0.0
    w = DFWriter()
    w.define("PARM", "QNf", "TimeUS,Name,Value")
    w.define("MSG", "QZ", "TimeUS,Message")
    w.define("EV", "QB", "TimeUS,Id")
    w.define("ARM", "QBIBB", "TimeUS,ArmState,ArmChecks,Forced,Method")
    w.define("MODE", "QMBB", "TimeUS,Mode,ModeNum,Rsn")
    w.define("ERR", "QBB", "TimeUS,Subsys,ECode")
    w.define("ATT", "QccccCCCC", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,ErrRP,ErrYaw")
    w.define("RATE", "Qfffffffff", "TimeUS,RDes,R,ROut,PDes,P,POut,YDes,Y,YOut")
    w.define("VIBE", "QBfffI", "TimeUS,IMU,VibeX,VibeY,VibeZ,Clip")
    w.define("BAT", "QBfffB", "TimeUS,Inst,Volt,Curr,CurrTot,RemPct")
    w.define("GPS", "QBBIHBcLLeff", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,VZ")
    w.define("POS", "QLLff", "TimeUS,Lat,Lng,Alt,RelHomeAlt")
    w.define("XKF4", "QBccccB", "TimeUS,C,SV,SP,SH,SM,PI")
    w.define("POWR", "QffH", "TimeUS,Vcc,VServo,Flags")
    w.define("MAG", "QBfff", "TimeUS,I,MagX,MagY,MagZ")
    w.define("RCOU", "Q" + "H" * 8, "TimeUS," + ",".join(f"C{i}" for i in range(1, 9)))
    w.define("CTUN", "Qf", "TimeUS,ThO")
    w.define("ISBH", "QHBBHHQf", "TimeUS,N,type,instance,mul,smp_cnt,SampleUS,smp_rate")
    w.define("ISBD", "QHHaaa", "TimeUS,N,seqno,x,y,z")

    def us(t: float) -> int:
        return BOOT_US + round(t * 1e6)

    capacity = level if kind == "low_reserve" else 5200.0
    params = {"MOT_PWM_MIN": 1000, "MOT_PWM_MAX": 2000, "MOT_BAT_VOLT_MAX": 4.2 * CELLS}
    params |= {"BATT_CAPACITY": capacity}
    params |= {"INS_LOG_BAT_MASK": 1, "INS_GYRO_FILTER": 80} | (extra or {})
    params |= {f"SERVO{ch}_FUNCTION": 32 + ch for ch in range(1, 5)}
    for name, value in params.items():
        w.write("PARM", TimeUS=us(0), Name=name, Value=value)
    w.write("MSG", TimeUS=us(0), Message="ArduCopter V4.6.2 (1ebd4d99)")
    w.write("MSG", TimeUS=us(0), Message="CubeOrange 0040003A3138511133383538")
    w.write("MSG", TimeUS=us(0), Message="Frame: QUAD/X")
    w.write("MODE", TimeUS=us(0), Mode=0, ModeNum=0, Rsn=0)

    arms = kind != "arming_refused"
    lifts = kind not in ("arming_refused", "takeoff_failure")
    crash_t = fault.start if kind == "crash" and fault else None
    end = crash_t + 2.0 if crash_t is not None else END

    def within(t: float) -> bool:
        return fault is not None and fault.start <= t < fault.end

    clip = 0
    used = 0.0
    # Rate loop per axis: a second-order system (natural frequency wn, damping
    # zeta) following stick steps (20-95 s; a calm hover after, which the motor
    # balance check needs), logged at 100 Hz. Healthy: wn 35 rad/s,
    # zeta 0.75 (peaks about 3% over). Tuning faults change the roll axis.
    wn = np.array([35.0, 35.0, 20.0])
    zeta = np.array([0.75, 0.75, 0.9])
    if kind == "tuning_overshoot":
        zeta[0] = level
    if kind == "tuning_sluggish":
        wn[0] = level
    rate_sp = np.zeros(3)
    rate_x = np.zeros(3)
    rate_v = np.zeros(3)
    next_step = np.zeros(3)
    earth = np.array([0.22, 0.05, 0.42])  # gauss, body frame (level, heading fixed)
    mag_per_amp = level if kind == "compass_interference" else 0.0004
    for k in range(int(end * 10)):
        t = k / 10
        if kind == "arming_refused" and k % 50 == 0 and t > 2:
            w.write("MSG", TimeUS=us(t), Message="PreArm: Gyros inconsistent")
        if arms and t == 5.0:
            w.write("EV", TimeUS=us(t), Id=10)
            w.write("ARM", TimeUS=us(t), ArmState=1)
        if lifts and t == 8.0:
            w.write("EV", TimeUS=us(t), Id=28)
        if kind == "takeoff_failure" and t == 8.0:
            w.write("MODE", TimeUS=us(t), Mode=4, ModeNum=4, Rsn=1)  # Guided takeoff
        if lifts and t == 20.0:
            w.write("MODE", TimeUS=us(t), Mode=5, ModeNum=5, Rsn=1)
        if crash_t is None and t == LAND:
            w.write("EV", TimeUS=us(t), Id=18)
        if arms and crash_t is None and t == DISARM:
            w.write("EV", TimeUS=us(t), Id=11)
            w.write("ARM", TimeUS=us(t), ArmState=0)
        if fault is not None and t == fault.start:
            if kind == "battery_failsafe":
                w.write("ERR", TimeUS=us(t), Subsys=6, ECode=1)
            if kind == "radio_failsafe":
                w.write("ERR", TimeUS=us(t), Subsys=5, ECode=1)
        if kind == "radio_failsafe" and fault is not None and t == fault.end:
            w.write("ERR", TimeUS=us(t), Subsys=5, ECode=0)

        armed = arms and 5 <= t < (DISARM if crash_t is None else end + 1)
        flying = lifts and 8 <= t < (LAND if crash_t is None else crash_t)
        alt = min(15.0, (t - 8) * 2.2) if flying else 0.0

        # attitude: the pilot (or Loiter) asks for small corrections, the vehicle follows
        des_roll = 3 * np.sin(0.4 * t) if flying else 0.0
        des_pitch = 2 * np.cos(0.3 * t) if flying else 0.0
        err = rng.normal(0, 0.4, 2) if flying else np.zeros(2)
        roll, pitch = des_roll + err[0], des_pitch + err[1]
        if kind == "attitude" and within(t):
            roll = des_roll + level * np.sign(np.sin(0.4 * t) + 1e-9)
        if crash_t is not None:
            if crash_t - 3 <= t < crash_t:
                roll = des_roll + (t - crash_t + 3) * 50  # flipping over
                alt = max(0.0, 15 - (t - crash_t + 3) * 5)
            elif t >= crash_t:
                roll, alt = 165.0, 0.0
        w.write(
            "ATT",
            TimeUS=us(t),
            DesRoll=des_roll,
            Roll=roll,
            DesPitch=des_pitch,
            Pitch=pitch,
            DesYaw=90,
            Yaw=90 + rng.normal(0, 0.5),
        )
        for j in range(10):
            tj = t + j * 0.01
            # stick and position-controller activity 20-95 s, steady hover after it
            if flying and 20 <= tj < 95:
                for a in range(3):
                    if tj >= next_step[a]:  # the pilot or position controller asks for a new rate
                        rate_sp[a] = rng.normal(0, 30.0 if a < 2 else 15.0)
                        next_step[a] = tj + rng.uniform(0.25, 0.8)
            else:
                rate_sp[:] = 0.0
            for _ in range(5):  # integrate at 500 Hz
                acc = wn**2 * (rate_sp - rate_x) - 2 * zeta * wn * rate_v
                rate_v += acc * 0.002
                rate_x += rate_v * 0.002
            gyro = rate_x + (rng.normal(0, 1.0, 3) if flying else 0.0)
            w.write(
                "RATE",
                TimeUS=us(tj),
                RDes=rate_sp[0],
                R=gyro[0],
                PDes=rate_sp[1],
                P=gyro[1],
                YDes=rate_sp[2],
                Y=gyro[2],
            )

        base = 14.0 if flying else 3.0
        vib = np.abs(rng.normal(base, base * 0.12, 3))
        if kind in ("vibration", "vibration_near") and within(t):
            vib[2] = abs(rng.normal(level, level * 0.06))
        if crash_t is not None and crash_t <= t < crash_t + 0.5:
            vib[:] = 95.0
            clip += 40
        if kind == "clipping" and within(t):
            clip += int(level)
        w.write("VIBE", TimeUS=us(t), IMU=0, VibeX=vib[0], VibeY=vib[1], VibeZ=vib[2], Clip=clip)

        cur = abs(rng.normal(22, 2)) if flying else (1.5 if armed else 0.4)
        cell = 4.1 - 0.15 * (t / END) - (0.012 * cur / 4 if flying else 0)
        if kind in ("battery_sag", "battery_near") and within(t):
            cur, cell = 70.0, level
        used += cur / 3.6 * 0.1  # mAh per 0.1 s
        volt = CELLS * cell + rng.normal(0, 0.02)
        remaining = max(0.0, 100 * (0.97 - used / capacity))  # took off on a 97% pack
        w.write(
            "BAT", TimeUS=us(t), Inst=0, Volt=volt, Curr=cur, CurrTot=used, RemPct=round(remaining)
        )
        vcc = 5.05 + rng.normal(0, 0.02)
        if kind == "brownout" and within(t):
            vcc = level + rng.normal(0, 0.01)
        if k % 2 == 0:  # POWR at 5 Hz, as ArduPilot logs it
            w.write("POWR", TimeUS=us(t), Vcc=vcc, VServo=0.0, Flags=0)
        b = earth + rng.normal(0, 0.003, 3)
        b[2] += mag_per_amp * cur  # the power wiring's own field grows with current
        w.write("MAG", TimeUS=us(t), I=0, MagX=b[0] * 1000, MagY=b[1] * 1000, MagZ=b[2] * 1000)

        sats, hdop, status = int(rng.integers(13, 17)), rng.normal(0.75, 0.04), 3
        if kind == "gps_degraded" and within(t):
            sats, hdop = int(level), 1.8
        if kind == "gps_loss" and within(t):
            sats, hdop, status = 3, 9.9, 1
        wk, ms = _gps_time(t + day * 86400)  # day: date the flight days apart
        lat = -35.3632 + 2e-6 * np.sin(0.02 * t)
        w.write(
            "GPS",
            TimeUS=us(t),
            I=0,
            Status=status,
            GMS=ms,
            GWk=wk,
            NSats=sats,
            HDop=hdop,
            Lat=lat,
            Lng=149.1652,
            Alt=584.0 + alt,
            Spd=1.0 if flying else 0,
        )
        w.write("POS", TimeUS=us(t), Lat=lat, Lng=149.1652, Alt=584 + alt, RelHomeAlt=alt)

        sv = abs(rng.normal(0.15, 0.05))
        if kind == "ekf" and within(t):
            sv = level
        w.write(
            "XKF4",
            TimeUS=us(t),
            C=0,
            SV=sv,
            SP=abs(rng.normal(0.12, 0.04)),
            SH=abs(rng.normal(0.1, 0.03)),
            SM=abs(rng.normal(0.08, 0.03)),
            PI=1 if kind == "lane_switch" and fault is not None and t >= fault.start else 0,
        )

        # gyro noise varies throttle so motor noise can be told from frame resonance
        thr = 0.5 + 0.15 * np.sin(2 * np.pi * t / 40) if kind == "gyro_noise" else 0.5
        if armed and k % 10 == 0:
            _gyro_burst(w, us(t), k // 10, rng, thr if flying else 0.1, fault, flying)
        if flying:
            pwm = 1000 + 1000 * thr + rng.normal(0, 12, 4)
            if kind == "motor_failure" and within(t):
                pwm[0], pwm[2] = 2000, 1250  # motor 1 pinned at full, its opposite backs off
            if kind == "imbalance":
                pwm[0] += level  # one motor works harder the whole flight (CG, bent arm)
        elif kind == "takeoff_failure" and armed and t >= 8:
            pwm = np.full(4, 1750.0)
        else:
            pwm = np.full(4, 1100.0 if armed else 1000.0)
        w.write("RCOU", TimeUS=us(t), C1=pwm[0], C2=pwm[1], C3=pwm[2], C4=pwm[3])
        tho = thr if flying else (0.75 if kind == "takeoff_failure" and armed and t >= 8 else 0.0)
        w.write("CTUN", TimeUS=us(t), ThO=tho)
    return w.save(path)


GYRO_RATE = 2000.0
GYRO_MUL = 32767 / np.radians(2000)  # int16 per rad/s, as ArduPilot's batch sampler


def _gyro_burst(
    w: DFWriter, t_us: int, n: int, rng: np.random.Generator, thr: float, fault: Fault | None,
    flying: bool,
) -> None:  # fmt: skip
    """One batch-sampler burst: 1024 gyro samples at 2 kHz (deg/s noise, motor tone)."""
    k = np.arange(1024)
    ts = k / GYRO_RATE
    axes = rng.normal(0, 0.8, (3, k.size))  # broadband sensor noise
    axes += 3 * np.sin(2 * np.pi * 2 * ts)[None, :]  # flight motion
    if flying:
        f0 = 120 * np.sqrt(thr / 0.5)  # motor noise follows rotor speed ~ sqrt(thrust)
        amp = fault.level if fault is not None and fault.kind == "gyro_noise" else 0.3
        axes[0] += amp * np.sin(2 * np.pi * f0 * ts)
        axes[1] += 0.7 * amp * np.sin(2 * np.pi * f0 * ts + 1)
        axes[0] += 0.3 * amp * np.sin(2 * np.pi * 2 * f0 * ts)
    raw = np.clip(np.round(np.radians(axes) * GYRO_MUL), -32768, 32767).astype("<i2")
    w.write("ISBH", TimeUS=t_us, N=n % 65536, type=1, instance=0, mul=round(GYRO_MUL),
            smp_cnt=k.size, SampleUS=t_us, smp_rate=GYRO_RATE)  # fmt: skip
    for seq in range(k.size // 32):
        part = raw[:, seq * 32 : (seq + 1) * 32]
        w.write("ISBD", TimeUS=t_us + seq, N=n % 65536, seqno=seq,
                x=part[0].tobytes(), y=part[1].tobytes(), z=part[2].tobytes())  # fmt: skip


# --- scenarios ---------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    fault: str
    make: Callable[[Path], Path]
    verdict: str | None = None
    expect: list[Any] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    crash_related: list[str] = field(default_factory=list)
    note: str = ""


def _win(check: str, severity: str, f: Fault, tol: float = 5.0) -> dict[str, Any]:
    return {"check": check, "severity": severity, "window": [f.start, f.end], "tolerance_s": tol}


def _copter(f: Fault | None, seed: int) -> Callable[[Path], Path]:
    return lambda p: copter(p, f, seed)


def scenarios() -> list[Scenario]:
    out: list[Scenario] = []
    for seed in range(3):
        out.append(Scenario(f"copter-healthy-{seed}", "healthy", _copter(None, seed), "healthy"))

    def add(name: str, fault: str, f: Fault, expect: list[Any], seed: int = 7, **kw: Any) -> None:
        out.append(Scenario(name, fault, _copter(f, seed), expect=expect, **kw))

    f = Fault("vibration", 60, 90, 42)
    add(
        "copter-vibration-attention",
        "vibration",
        f,
        [_win("vibration.level", "attention", f)],
        verdict="attention",
    )
    f = Fault("vibration", 60, 90, 75)
    add(
        "copter-vibration-problem",
        "vibration",
        f,
        [_win("vibration.level", "problem", f)],
        verdict="problem",
    )
    f = Fault("vibration_near", 60, 90, 25)
    add("copter-vibration-below-limit", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("clipping", 50, 70, 20)
    add("copter-clipping", "clipping", f, [_win("imu.clipping", "problem", f)], verdict="problem")
    f = Fault("battery_sag", 100, 115, 3.15)
    add("copter-battery-sag", "battery", f, [_win("battery.sag", "problem", f)], verdict="problem")
    f = Fault("battery_near", 100, 115, 3.6)
    add("copter-battery-above-limit", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("battery_failsafe", 120, 121)
    add(
        "copter-battery-failsafe",
        "failsafe",
        f,
        [_win("events.failsafes", "problem", f, 2)],
        verdict="problem",
    )
    f = Fault("radio_failsafe", 90, 96)
    add(
        "copter-radio-failsafe",
        "failsafe",
        f,
        [_win("events.failsafes", "problem", f, 2)],
        verdict="problem",
    )
    f = Fault("gps_degraded", 70, 100, 7)
    add("copter-gps-degraded", "GPS", f, [_win("gps.quality", "attention", f)], verdict="attention")
    f = Fault("gps_loss", 70, 85)
    add("copter-gps-loss", "GPS", f, [_win("gps.quality", "problem", f)], verdict="problem")
    f = Fault("ekf", 80, 88, 1.6)
    add(
        "copter-ekf-velocity",
        "estimator",
        f,
        [_win("estimator.health", "problem", f)],
        verdict="problem",
    )
    f = Fault("motor_failure", 60, 80)
    # Imbalance is a whole-flight average, so a 20 s failure may raise it at any severity.
    add(
        "copter-motor-failure",
        "motor",
        f,
        [_win("motor.saturation", "problem", f)],
        verdict="problem",
        allow=["motor.imbalance"],
    )
    f = Fault("imbalance", level=80)
    add("copter-motor-imbalance", "motor", f, ["motor.imbalance:attention"], verdict="attention")
    f = Fault("imbalance", level=25)
    add("copter-motor-imbalance-small", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("attitude", 60, 75, 14)
    add(
        "copter-attitude-tracking",
        "attitude",
        f,
        [_win("attitude.tracking", "problem", f)],
        verdict="problem",
    )
    f = Fault("crash", 120)
    add(
        "copter-crash",
        "crash",
        f,
        [{"check": "flight.crash", "severity": "problem", "window": [117, 122], "tolerance_s": 3}],
        verdict="problem",
        allow=["attitude.tracking", "events.failsafes", "control.attitude_limits"],
    )
    f = Fault("gyro_noise", level=12.0)
    add("copter-gyro-noise", "gyro noise", f, ["vibration.spectrum:attention"],
        verdict="attention")  # fmt: skip
    notch = {"INS_HNTCH_ENABLE": 1, "INS_HNTCH_MODE": 1, "INS_HNTCH_FREQ": 120,
             "INS_HNTCH_BW": 60, "INS_HNTCH_REF": 0.5}  # fmt: skip
    out.append(Scenario("copter-gyro-noise-notched", "healthy (near limit)",
                        lambda p: copter(p, Fault("gyro_noise", level=12.0), 7, notch),
                        "healthy"))  # fmt: skip
    # rate-loop tuning (whole flight, roll axis): damping 0.2 peaks at 1.53, 0.08 at 1.78
    f = Fault("tuning_overshoot", level=0.2)
    add("copter-tuning-overshoot", "tuning", f, ["control.tuning:attention"], verdict="attention")
    f = Fault("tuning_overshoot", level=0.08)
    add("copter-tuning-oscillating", "tuning", f, ["control.tuning:problem"], verdict="problem")
    f = Fault("tuning_overshoot", level=0.45)
    add("copter-tuning-near-limit", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("tuning_sluggish", level=5.0)
    add("copter-tuning-sluggish", "tuning", f, ["control.tuning:attention"], verdict="attention")
    f = Fault("brownout", 60, 75, 4.35)
    add("copter-brownout", "power", f, [_win("power.supply", "attention", f)], verdict="attention")
    f = Fault("brownout", 60, 75, 4.2)
    add("copter-brownout-severe", "power", f, [_win("power.supply", "problem", f)],
        verdict="problem")  # fmt: skip
    f = Fault("brownout", 60, 75, 4.6)
    add("copter-supply-low-but-ok", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("low_reserve", level=1100)
    add("copter-low-reserve", "battery", f, ["battery.reserve:attention"], verdict="attention")
    f = Fault("low_reserve", level=1000)
    add("copter-empty-battery", "battery", f, ["battery.reserve:problem"], verdict="problem")
    f = Fault("low_reserve", level=1300)
    add("copter-reserve-near-limit", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("compass_interference", level=0.0074)
    add("copter-compass-interference", "compass", f, ["compass.interference:attention"],
        verdict="attention")  # fmt: skip
    f = Fault("compass_interference", level=0.015)
    add("copter-compass-interference-severe", "compass", f, ["compass.interference:problem"],
        verdict="problem")  # fmt: skip
    f = Fault("compass_interference", level=0.0037)
    add("copter-compass-near-limit", "healthy (near limit)", f, [], verdict="healthy")
    f = Fault("lane_switch", 70, 75)
    add("copter-lane-switch", "estimator", f, [_win("estimator.lanes", "attention", f, 2)],
        verdict="attention")  # fmt: skip
    f = Fault("takeoff_failure")
    add("copter-takeoff-failure", "never flew", f, ["flight.takeoff:problem"], verdict="problem")
    f = Fault("arming_refused")
    add(
        "copter-arming-refused",
        "never flew",
        f,
        ["preflight.arming:attention"],
        verdict="insufficient data",
    )

    def plane(**kw: Any) -> Callable[[Path], Path]:
        return lambda p: plane_log(p, **kw)

    out += [
        Scenario("plane-healthy", "healthy", plane(ending="smooth"), "healthy"),
        Scenario(
            "plane-out-of-trim",
            "trim",
            plane(ending="smooth", trim=-0.2),
            "attention",
            expect=["control.trim:attention"],
        ),
        Scenario(
            "plane-hard-landing",
            "hard landing",
            plane(ending="hard"),
            "attention",
            expect=[
                {
                    "check": "flight.crash",
                    "severity": "attention",
                    "window": [99, 101],
                    "tolerance_s": 3,
                }
            ],
        ),
        Scenario(
            "plane-crash",
            "crash",
            plane(ending="crash", trim=-0.2),
            "problem",
            expect=[
                {
                    "check": "flight.crash",
                    "severity": "problem",
                    "window": [88, 91],
                    "tolerance_s": 3,
                },
                # one finding per flight, anchored at the worst excursion (-134° at 85-89 s)
                {"check": "control.attitude_limits", "severity": "problem", "window": [84, 89.5]},
                "control.saturation",
                "control.trim:attention",
            ],
            allow=["events.failsafes"],
            crash_related=[
                "Bank angle beyond the 45° limit",
                "Aileron at full travel",
                "Aircraft out of trim (roll)",
            ],
        ),
    ]
    return out


def build(out_dir: Path) -> Path:
    """Write every scenario log and ``benchmark.yaml`` into ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    logs: dict[str, Any] = {}
    for s in scenarios():
        name = f"{s.name}.bin"
        s.make(out_dir / name)
        entry: dict[str, Any] = {"fault": s.fault, "exhaustive": True, "expect": s.expect}
        if s.verdict:
            entry["verdict"] = s.verdict
        if s.allow:
            entry["allow"] = s.allow
        if s.crash_related:
            entry["crash_related"] = s.crash_related
        logs[name] = entry
    manifest = {
        "source": "generated by flightlens.sim.benchmark (ArduPilot DataFlash, seeded noise)",
        "logs_dir": ".",
        "logs": logs,
    }
    path = out_dir / "benchmark.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path
