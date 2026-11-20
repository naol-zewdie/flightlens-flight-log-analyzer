"""ArduPilot DataFlash (.bin) adapter.

Simple field-to-signal mappings live in ``mappings/ardupilot.yaml``. Signals
that need computation are built here:

- ``vibe.x/y/z`` (m/s²) from the IMU with the worst vibration, and
  ``vibe.clip`` (cumulative count) summed over all IMUs
- ``mot.1..N`` (0-1) from RCOU PWM, using SERVOn_FUNCTION to find each motor;
  a QuadPlane forward throttle becomes ``pusher.1``
- ``state.landed`` from the land detector events, ``state.att_ctrl`` from the
  flight mode, ``bat.cells`` when it can be determined reliably
- armed segments, events (arming, modes, ERR failsafes, messages), metadata

Time is seconds since the log's first timestamp.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from flightlens.model import (
    Event,
    EventKind,
    Flight,
    FlightMetadata,
    FloatArray,
    ParseIssue,
    Segment,
    Signal,
)
from flightlens.parsers import dataflash
from flightlens.parsers._mapping import (
    TopicKey,
    apply_mapping,
    arming_quality,
    load_mapping,
    mapping_topics,
)
from flightlens.parsers.dataflash import Columns, DataFlashLog, DataFlashParseError

__all__ = ["DataFlashParseError", "parse"]

# --- vehicle tables ---------------------------------------------------------

COPTER_MODES: dict[int, str] = {
    0: "Stabilize",
    1: "Acro",
    2: "AltHold",
    3: "Auto",
    4: "Guided",
    5: "Loiter",
    6: "RTL",
    7: "Circle",
    9: "Land",
    11: "Drift",
    13: "Sport",
    14: "Flip",
    15: "AutoTune",
    16: "PosHold",
    17: "Brake",
    18: "Throw",
    19: "Avoid ADSB",
    20: "Guided NoGPS",
    21: "Smart RTL",
    22: "FlowHold",
    23: "Follow",
    24: "ZigZag",
    25: "SystemID",
    26: "Heli Autorotate",
    27: "Auto RTL",
    28: "Turtle",
}
PLANE_MODES: dict[int, str] = {
    0: "Manual",
    1: "Circle",
    2: "Stabilize",
    3: "Training",
    4: "Acro",
    5: "FBWA",
    6: "FBWB",
    7: "Cruise",
    8: "Autotune",
    10: "Auto",
    11: "RTL",
    12: "Loiter",
    13: "Takeoff",
    14: "Avoid ADSB",
    15: "Guided",
    17: "QStabilize",
    18: "QHover",
    19: "QLoiter",
    20: "QLand",
    21: "QRTL",
    22: "QAutotune",
    23: "QAcro",
    24: "Thermal",
    25: "Loiter to QLand",
}
ROVER_MODES: dict[int, str] = {
    0: "Manual",
    1: "Acro",
    3: "Steering",
    4: "Hold",
    5: "Loiter",
    6: "Follow",
    7: "Simple",
    10: "Auto",
    11: "RTL",
    12: "Smart RTL",
    15: "Guided",
}
MODES_BY_FAMILY = {"Copter": COPTER_MODES, "Plane": PLANE_MODES, "Rover": ROVER_MODES}
# Modes without an attitude setpoint (the pilot commands rates or surfaces).
NO_ATTITUDE_CONTROL = {"Copter": {1, 28}, "Plane": {0, 3, 4, 23}, "Rover": set(range(64))}

MODE_REASONS: dict[int, str] = {
    1: "RC command",
    2: "GCS command",
    3: "radio failsafe",
    4: "battery failsafe",
    5: "GCS failsafe",
    6: "EKF failsafe",
    7: "GPS glitch",
    8: "mission end",
    9: "throttle land escape",
    10: "fence breached",
    11: "terrain failsafe",
}

FAMILIES = {
    "ArduCopter": "Copter",
    "ArduHeli": "Copter",
    "ArduPlane": "Plane",
    "ArduRover": "Rover",
    "Rover": "Rover",
    "ArduSub": "Sub",
    "Blimp": "Blimp",
}
FRAMES: dict[str, str] = {
    "QUAD": "Quadrotor",
    "HEXA": "Hexarotor",
    "OCTA": "Octorotor",
    "OCTAQUAD": "Octorotor (X8)",
    "Y6": "Hexarotor (Y6)",
    "TRI": "Tricopter",
    "HELI": "Helicopter",
    "HELI_DUAL": "Helicopter",
    "DODECAHEXA": "Dodecarotor",
    "DECA": "Decarotor",
    "SINGLE": "Single rotor",
    "COAX": "Coaxial",
}

# ERR subsystems (AP_Logger LogErrorSubsystem). ECode 0 always means "resolved".
ERR_SUBSYSTEMS: dict[int, str] = {
    1: "Main",
    2: "Radio",
    3: "Compass",
    4: "Optical flow",
    5: "Radio failsafe",
    6: "Battery failsafe",
    7: "GPS failsafe",
    8: "GCS failsafe",
    9: "Fence failsafe",
    10: "Flight mode change refused",
    11: "GPS",
    12: "Crash check",
    13: "Flip",
    14: "Autotune",
    15: "Parachute",
    16: "EKF check",
    17: "EKF/inertial nav failsafe",
    18: "Barometer",
    19: "CPU",
    20: "ADS-B failsafe",
    21: "Terrain",
    22: "Navigation",
    23: "Terrain failsafe",
    24: "EKF primary changed",
    25: "Thrust loss check",
    26: "Sensor failsafe",
    27: "Leak failsafe",
    28: "Pilot input",
    29: "Vibration failsafe",
    30: "Internal error",
    31: "Dead-reckoning failsafe",
}
FAILSAFE_SUBSYSTEMS = {5, 6, 7, 8, 9, 17, 20, 23, 26, 27, 29, 31}
CRITICAL_ERRORS = {(12, 1): "crash detected", (12, 2): "loss of control", (25, 1): "thrust loss"}
INFO_SUBSYSTEMS = {14, 24}  # autotune and EKF lane switches are not faults

# EV ids (AP_Logger LogEvent)
EV_ARMED, EV_DISARMED = 10, 11
EV_LAND_COMPLETE, EV_NOT_LANDED = 18, 28

# SERVOn_FUNCTION values
MOTOR_FUNCTIONS = {**{33 + i: i + 1 for i in range(8)}, **{82 + i: 9 + i for i in range(4)}}
THROTTLE_FUNCTION = 70

EXPECTED_MESSAGES: dict[str, tuple[str, ...]] = {
    "arming": ("ARM", "EV"),
    "attitude": ("ATT",),
    "vibration": ("VIBE",),
    "battery": ("BAT", "CURR"),
    "GPS": ("GPS",),
    "motor outputs": ("RCOU",),
    "estimator status": ("XKF4", "NKF4"),
}

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=UTC)
GPS_LEAP_SECONDS = 18
FIRMWARE_RE = re.compile(
    r"^(ArduCopter|ArduPlane|ArduRover|Rover|ArduSub|ArduHeli|Blimp|AntennaTracker)"
    r"\s+V?([\w.\-]+)(?:\s*\(([0-9a-fA-F]+)\))?"
)
# "CubeOrange 0040003A3138511133383538" or "CUAVv5-bdshot 00270035 3156500F 20333543"
BOARD_RE = re.compile(r"^([A-Za-z][\w\-]*)\s+((?:[0-9A-F]{8}\s?){3})(?:\s|$)")


def parse(path: Path) -> Flight:
    mapping = load_mapping("ardupilot")
    wanted = mapping_topics(mapping)
    log = dataflash.read(path, wanted)
    if not log.messages:
        raise DataFlashParseError("the log contains no timestamped data")

    quality: list[ParseIssue] = list(log.issues)
    signals: dict[str, Signal] = {}
    split: dict[str, dict[int, Columns]] = {}

    def cols_of(topic: str, instance: int) -> Columns | None:
        if topic not in split:
            split[topic] = log.instances(topic)
        return split[topic].get(instance)

    def add(name: str, unit: str, key: TopicKey, values: FloatArray, source: str) -> None:
        cols = cols_of(*key)
        if cols is None:
            return
        _add_signal(signals, quality, log, name, unit, cols, values, source)

    def lookup(topic: str, instance: int, field: str) -> FloatArray | None:
        cols = cols_of(topic, instance)
        if cols is None or field not in cols or "TimeUS" not in cols:
            return None
        return np.asarray(cols[field], dtype=np.float64)

    # 1. Metadata first: the vehicle family selects the mode table.
    messages = _messages(log)
    params, param_events = _parameters(log)
    firmware, family = _firmware(log, messages)
    vehicle_type = _vehicle_type(family, messages, params)

    # 2. Table-driven signals.
    apply_mapping(mapping, lookup, add)

    # 3. Computed signals.
    _vibration(log, signals, quality)
    _raw_gyro(log, signals)
    _motors(log, signals, quality, params)
    segments, arm_events, arming_known = _arming(log)
    mode_events, att_ctrl = _modes(log, family)
    if att_ctrl is not None:
        signals["state.att_ctrl"] = att_ctrl
    landed = _landed(log, segments)
    if landed is not None:
        signals["state.landed"] = landed
    _cells(signals, quality, params, segments)

    events = arm_events + mode_events + _errors(log) + param_events + _crash_flag(log)
    events += [Event(t, *_message_kind(text), "MSG") for t, text in messages]
    events.sort(key=lambda e: e.t)

    # 4. Data quality.
    _dropouts(log, quality)
    for label, names in EXPECTED_MESSAGES.items():
        if not any(log.counts.get(n) for n in names):
            quality.append(
                ParseIssue("missing_topic", f"No {label} data ({' or '.join(names)} messages)")
            )
    arming_quality(events, segments, arming_known, quality)

    hardware, serial = _hardware(messages)
    metadata = FlightMetadata(
        log_format="dataflash",
        source_path=path,
        vehicle_type=vehicle_type,
        firmware=firmware,
        hardware=hardware,
        serial=serial,
        start_time=_utc_start(log),
        log_duration=(log.end_us - log.start_us) / 1e6,
        mapping_version=str(mapping["version"]),
    )
    return Flight(
        metadata=metadata,
        signals=signals,
        events=events,
        parameters=params,
        segments=segments,
        quality=quality,
        arming_known=arming_known,
    )


# --- helpers ----------------------------------------------------------------


def _add_signal(
    signals: dict[str, Signal],
    quality: list[ParseIssue],
    log: DataFlashLog,
    name: str,
    unit: str,
    cols: Columns,
    values: FloatArray,
    source: str,
) -> None:
    raw_t = cols["TimeUS"]
    keep = raw_t > 0  # rows written before the clock started
    t = log.time_s(cols)[keep]
    values = np.asarray(values, dtype=np.float64)[keep]
    if not values.size or not np.any(np.isfinite(values)):
        quality.append(ParseIssue("invalid_values", f"{name}: no valid samples in {source}"))
        return
    if t.size > 1 and np.any(np.diff(t) < 0):
        quality.append(ParseIssue("non_monotonic", f"{source}: timestamps go backwards"))
        order = np.argsort(t, kind="stable")
        t, values = t[order], values[order]
    signals[name] = Signal(name=name, unit=unit, t=t, values=values, source=source)


def _step_signal(name: str, unit: str, t: list[float], v: list[float], source: str) -> Signal:
    return Signal(
        name=name,
        unit=unit,
        t=np.asarray(t, dtype=np.float64),
        values=np.asarray(v, dtype=np.float64),
        source=source,
    )


def _times(log: DataFlashLog, cols: Columns) -> FloatArray:
    return np.maximum(log.time_s(cols), 0.0)


# --- vibration and motors ---------------------------------------------------


def _raw_gyro(log: DataFlashLog, signals: dict[str, Signal]) -> None:
    """High-rate gyro for the noise spectrum: GYR (raw logging) or the batch sampler.

    The batch sampler (INS_LOG_BAT_MASK) logs bursts of samples: ISBH heads each
    burst (sensor type 1 = gyro, instance, multiplier, first sample time, rate)
    and ISBD carries 32 samples per axis as int16, value = raw / multiplier (rad/s).
    """
    gyr = log.instances("GYR").get(0)
    if gyr is not None and all(k in gyr for k in ("GyrX", "GyrY", "GyrZ")):
        us = gyr.get("SampleUS", gyr["TimeUS"]).astype(np.float64)
        t = (us - log.start_us) / 1e6
        values = {a: np.degrees(gyr[f"Gyr{a.upper()}"].astype(np.float64)) for a in "xyz"}
        _gyro_signals(signals, t, values, "GYR[0] raw gyro")
        return

    hdr, data = log.messages.get("ISBH"), log.messages.get("ISBD")
    need_h = ("N", "type", "instance", "mul", "SampleUS", "smp_rate")
    if hdr is None or data is None or not all(k in hdr for k in need_h):
        return
    if not all(k in data for k in ("N", "seqno", "x", "y", "z")):
        return
    gyro = (hdr["type"] == 1) & (hdr["instance"] == 0) & (hdr["mul"] > 0) & (hdr["smp_rate"] > 0)
    if not np.any(gyro):
        return
    h_n = hdr["N"][gyro].astype(np.int64)
    order = np.argsort(h_n)
    h_n = h_n[order]
    h_us = hdr["SampleUS"][gyro][order].astype(np.float64)
    h_rate = hdr["smp_rate"][gyro][order].astype(np.float64)
    h_mul = hdr["mul"][gyro][order].astype(np.float64)
    d_n = data["N"].astype(np.int64)
    idx = np.clip(np.searchsorted(h_n, d_n), 0, h_n.size - 1)
    ok = h_n[idx] == d_n
    if not np.any(ok):
        return
    idx = idx[ok]
    k = data["seqno"][ok].astype(np.float64)[:, None] * 32 + np.arange(32)[None, :]
    t = ((h_us[idx][:, None] + k / h_rate[idx][:, None] * 1e6) - log.start_us) / 1e6
    mul = h_mul[idx][:, None]
    values = {a: np.degrees(data[a][ok] / mul).ravel() for a in "xyz"}
    t = t.ravel()
    order = np.argsort(t, kind="stable")
    _gyro_signals(
        signals,
        t[order],
        {a: v[order] for a, v in values.items()},
        "ISBD batch sampler, gyro 0 (bursts)",
    )


def _gyro_signals(
    signals: dict[str, Signal], t: FloatArray, values: dict[str, FloatArray], source: str
) -> None:
    if t.size < 64:
        return
    for axis, v in values.items():
        signals[f"gyro.raw.{axis}"] = Signal(
            f"gyro.raw.{axis}", "deg/s", t, np.asarray(v, dtype=np.float64), source
        )


def _vibration(log: DataFlashLog, signals: dict[str, Signal], quality: list[ParseIssue]) -> None:
    imus = log.instances("VIBE")
    if not imus:
        return
    # Worst IMU: highest 95th percentile of its worst axis.
    best: tuple[float, int] | None = None
    for inst, c in imus.items():
        if not all(k in c for k in ("VibeX", "VibeY", "VibeZ")):
            continue
        worst_axis = np.maximum(np.maximum(c["VibeX"], c["VibeY"]), c["VibeZ"]).astype(np.float64)
        finite = worst_axis[np.isfinite(worst_axis)]
        if finite.size:
            score = float(np.percentile(finite, 95))
            if best is None or score > best[0]:
                best = (score, inst)
    n = len(imus)
    if best is not None:
        c = imus[best[1]]
        for axis in ("X", "Y", "Z"):
            _add_signal(
                signals,
                quality,
                log,
                f"vibe.{axis.lower()}",
                "m/s²",
                c,
                c[f"Vibe{axis}"].astype(np.float64),
                f"VIBE[{best[1]}].Vibe{axis} (worst of {n} IMU)",
            )

    # Clipping: one counter per IMU (Clip), or Clip0..2 in one message (older logs).
    counters: list[tuple[FloatArray, FloatArray]] = []
    for c in imus.values():
        t = _times(log, c)
        if "Clip" in c:
            counters.append((t, c["Clip"].astype(np.float64)))
        else:
            for k in ("Clip0", "Clip1", "Clip2"):
                if k in c:
                    counters.append((t, c[k].astype(np.float64)))
    if not counters:
        return
    ref_t = max(counters, key=lambda tc: tc[0].size)[0]
    total = np.zeros(ref_t.shape)
    for t, v in counters:
        idx = np.searchsorted(t, ref_t, side="right") - 1
        total += np.where(idx >= 0, v[np.clip(idx, 0, v.size - 1)], 0.0)
    signals["vibe.clip"] = Signal(
        "vibe.clip", "count", ref_t, total, f"VIBE clipping counters (sum of {len(counters)})"
    )


def _motors(
    log: DataFlashLog,
    signals: dict[str, Signal],
    quality: list[ParseIssue],
    params: dict[str, float],
) -> None:
    outputs: dict[int, tuple[Columns, str]] = {}
    for msg in ("RCOU", "RCO2"):
        cols = log.messages.get(msg)
        if cols is None:
            continue
        for col in cols:
            if col.startswith("C") and col[1:].isdigit():
                outputs[int(col[1:])] = (cols, f"{msg}.{col}")
    if not outputs:
        return

    motors: dict[int, int] = {}  # motor number -> servo channel
    throttle: int | None = None
    for ch in range(1, 33):
        fn = int(params.get(f"SERVO{ch}_FUNCTION", -1))
        if fn in MOTOR_FUNCTIONS:
            motors.setdefault(MOTOR_FUNCTIONS[fn], ch)
        elif fn == THROTTLE_FUNCTION and throttle is None:
            throttle = ch
    if not motors:
        return  # fixed wing, rover, or parameters not logged

    lo, hi = _pwm_range(params)
    if hi <= lo:
        quality.append(ParseIssue("invalid_values", f"Motor PWM range {lo:g}-{hi:g} is invalid"))
        return
    for i, (motor, ch) in enumerate(sorted(motors.items()), start=1):
        if ch not in outputs:
            continue
        cols, src = outputs[ch]
        vals = (cols[f"C{ch}"].astype(np.float64) - lo) / (hi - lo)
        _add_signal(
            signals,
            quality,
            log,
            f"mot.{i}",
            "fraction",
            cols,
            vals,
            f"{src} (motor {motor}, PWM {lo:g}-{hi:g})",
        )
    if throttle is not None and throttle in outputs:
        cols, src = outputs[throttle]
        tlo = params.get(f"SERVO{throttle}_MIN", 1000.0)
        thi = params.get(f"SERVO{throttle}_MAX", 2000.0)
        if thi > tlo:
            vals = (cols[f"C{throttle}"].astype(np.float64) - tlo) / (thi - tlo)
            _add_signal(
                signals, quality, log, "pusher.1", "fraction", cols, vals, f"{src} (throttle)"
            )


def _pwm_range(params: dict[str, float]) -> tuple[float, float]:
    for lo_p, hi_p in (
        ("MOT_PWM_MIN", "MOT_PWM_MAX"),
        ("Q_M_PWM_MIN", "Q_M_PWM_MAX"),
        ("RC3_MIN", "RC3_MAX"),
    ):
        lo, hi = params.get(lo_p, 0.0), params.get(hi_p, 0.0)
        if lo > 0 and hi > lo:
            return lo, hi
    return 1000.0, 2000.0


# --- arming, landing, modes -------------------------------------------------


def _arming(log: DataFlashLog) -> tuple[list[Segment], list[Event], bool]:
    changes: list[tuple[float, bool, str]] = []
    arm = log.messages.get("ARM")
    if arm is not None and "ArmState" in arm:
        for t, s in zip(_times(log, arm).tolist(), arm["ArmState"].tolist(), strict=True):
            changes.append((t, bool(s), "ARM.ArmState"))
    else:
        ev = log.messages.get("EV")
        if ev is not None and "Id" in ev:
            for t, i in zip(_times(log, ev).tolist(), ev["Id"].tolist(), strict=True):
                if i in (EV_ARMED, EV_DISARMED):
                    changes.append((t, i == EV_ARMED, "EV"))
    stat = log.messages.get("STAT")
    if not changes and stat is not None and "Armed" in stat:
        # Some logs have no ARM/EV arming records; Plane's STAT.Armed still does.
        armed_col = stat["Armed"].astype(bool)
        t_stat = _times(log, stat)
        edges = np.flatnonzero(np.r_[True, armed_col[1:] != armed_col[:-1]])
        for i in edges:
            changes.append((float(t_stat[i]), bool(armed_col[i]), "STAT.Armed"))
    # ArduPilot always logs an arming event, so if the message types exist the
    # arming state is known, even when the aircraft never armed.
    arming_known = (
        bool(changes) or "ARM" in log.formats or "EV" in log.formats or "STAT" in log.formats
    )
    end = (log.end_us - log.start_us) / 1e6
    segments: list[Segment] = []
    events: list[Event] = []
    armed_at: float | None = None
    for i, (t, armed, src) in enumerate(sorted(changes)):
        if armed and armed_at is None:
            armed_at = t
            events.append(Event(t, EventKind.ARM, "Armed", source=src))
        elif not armed:
            if armed_at is None and i == 0:
                armed_at = 0.0  # logging started after arming
            if armed_at is not None:
                segments.append(Segment(armed_at, t))
                events.append(Event(t, EventKind.DISARM, "Disarmed", source=src))
                armed_at = None
    if armed_at is not None:
        segments.append(Segment(armed_at, end))
        events.append(
            Event(end, EventKind.ERROR, "Log ends while armed", level="warning", source="ARM")
        )
    return segments, events, arming_known


def _landed(log: DataFlashLog, segments: list[Segment]) -> Signal | None:
    ev = log.messages.get("EV")
    ids = ev["Id"].astype(np.int64) if ev is not None and "Id" in ev else np.array([], np.int64)
    if ev is None or not np.isin(ids, (EV_LAND_COMPLETE, EV_NOT_LANDED)).any():
        return _landed_from_stat(log)
    points = [(0.0, 1.0)]
    for t, i in zip(_times(log, ev).tolist(), ids.tolist(), strict=True):
        if i == EV_NOT_LANDED:
            points.append((t, 0.0))
        elif i == EV_LAND_COMPLETE:
            points.append((t, 1.0))
    points += [(s.end, 1.0) for s in segments]  # disarmed means on the ground
    points.sort(key=lambda p: p[0])
    t, v = zip(*points, strict=True)
    return _step_signal("state.landed", "flag", list(t), list(v), "EV land detector events")


def _landed_from_stat(log: DataFlashLog) -> Signal | None:
    """Plane: STAT.isFlying is the flight controller's own airborne estimate."""
    stat = log.messages.get("STAT")
    if stat is None or "isFlying" not in stat or "TimeUS" not in stat:
        return None
    t = _times(log, stat)
    landed = 1.0 - stat["isFlying"].astype(np.float64)
    return Signal("state.landed", "flag", t, landed, "STAT.isFlying (inverted)")


def _crash_flag(log: DataFlashLog) -> list[Event]:
    """Plane's own crash detector (STAT.Crash); off unless CRASH_DETECT is set."""
    stat = log.messages.get("STAT")
    if stat is None or "Crash" not in stat:
        return []
    crash = stat["Crash"].astype(np.int64) > 0
    rising = np.flatnonzero(crash & ~np.r_[False, crash[:-1]])
    t = _times(log, stat)
    return [
        Event(float(t[i]), EventKind.ERROR, "Firmware crash detector", "critical", "STAT.Crash")
        for i in rising
    ]


def _modes(log: DataFlashLog, family: str | None) -> tuple[list[Event], Signal | None]:
    mode = log.messages.get("MODE")
    if mode is None:
        return [], None
    col = "ModeNum" if "ModeNum" in mode else "Mode" if "Mode" in mode else None
    if col is None:
        return [], None
    names = MODES_BY_FAMILY.get(family or "", {})
    no_att = NO_ATTITUDE_CONTROL.get(family or "")
    t = _times(log, mode).tolist()
    nums = mode[col].astype(np.int64).tolist()
    reasons = mode["Rsn"].astype(np.int64).tolist() if "Rsn" in mode else [0] * len(nums)
    events = []
    for ti, n, r in zip(t, nums, reasons, strict=True):
        why = MODE_REASONS.get(r)
        src = f"MODE.{col}" + (f" ({why})" if why else "")
        events.append(Event(ti, EventKind.MODE, names.get(n, f"Mode {n}"), source=src))
    att = None
    if no_att is not None and t:
        att = _step_signal(
            "state.att_ctrl",
            "flag",
            [0.0, *t],
            [float(nums[0] not in no_att)] + [float(n not in no_att) for n in nums],
            f"MODE.{col} (attitude-controlled modes)",
        )
    return events, att


# --- events -----------------------------------------------------------------


def _errors(log: DataFlashLog) -> list[Event]:
    err = log.messages.get("ERR")
    if err is None or "Subsys" not in err or "ECode" not in err:
        return []
    events = []
    rows = zip(
        _times(log, err).tolist(),
        err["Subsys"].astype(np.int64).tolist(),
        err["ECode"].astype(np.int64).tolist(),
        strict=True,
    )
    for t, sub, code in rows:
        name = ERR_SUBSYSTEMS.get(sub, f"Subsystem {sub}")
        if code == 0:
            events.append(Event(t, EventKind.MESSAGE, f"{name}: resolved", "info", "ERR"))
        elif (sub, code) in CRITICAL_ERRORS:
            text = f"{name}: {CRITICAL_ERRORS[(sub, code)]}"
            events.append(Event(t, EventKind.ERROR, text, "critical", "ERR"))
        elif sub in FAILSAFE_SUBSYSTEMS:
            events.append(Event(t, EventKind.FAILSAFE, name, "critical", f"ERR code {code}"))
        elif sub in INFO_SUBSYSTEMS:
            events.append(Event(t, EventKind.MESSAGE, f"{name} (code {code})", "info", "ERR"))
        else:
            events.append(Event(t, EventKind.ERROR, f"{name} (code {code})", "error", "ERR"))
    return events


def _messages(log: DataFlashLog) -> list[tuple[float, str]]:
    msg = log.messages.get("MSG")
    if msg is None or "Message" not in msg:
        return []
    t = _times(log, msg).tolist()
    return [
        (ti, " ".join(text.split())) for ti, text in zip(t, log.text("MSG", "Message"), strict=True)
    ]


def _message_kind(text: str) -> tuple[EventKind, str, str]:
    level = "warning" if text.startswith(("PreArm", "Arm:")) else "info"
    return EventKind.MESSAGE, text, level


def _parameters(log: DataFlashLog) -> tuple[dict[str, float], list[Event]]:
    parm = log.messages.get("PARM")
    if parm is None or "Name" not in parm or "Value" not in parm:
        return {}, []
    params: dict[str, float] = {}
    events = []
    t = _times(log, parm).tolist()
    for ti, name, value in zip(t, log.text("PARM", "Name"), parm["Value"].tolist(), strict=True):
        v = float(value)
        if name not in params:
            params[name] = v
        elif params[name] != v and ti > 0:
            events.append(
                Event(ti, EventKind.MESSAGE, f"Parameter {name} changed to {v:g}", source="PARM")
            )
    return params, events


# --- metadata and quality ---------------------------------------------------


def _firmware(
    log: DataFlashLog, messages: list[tuple[float, str]]
) -> tuple[str | None, str | None]:
    texts = log.text("VER", "FWS") + [text for _, text in messages[:40]]
    for text in texts:
        m = FIRMWARE_RE.match(text)
        if m:
            vehicle, version, git = m.groups()
            fw = f"{vehicle} V{version.lstrip('Vv')}" + (f" {git[:8]}" if git else "")
            return fw, FAMILIES.get(vehicle)
    return None, None


def _vehicle_type(
    family: str | None, messages: list[tuple[float, str]], params: dict[str, float]
) -> str | None:
    for _, text in messages[:60]:
        if text.startswith("Frame:"):
            frame = text.split(":", 1)[1].strip().split("/")[0].split()[0].upper()
            if frame in FRAMES:
                return FRAMES[frame]
    if family == "Plane":
        return "VTOL (QuadPlane)" if params.get("Q_ENABLE", 0) > 0 else "Fixed wing"
    if family == "Rover":
        return "Ground rover"
    if family == "Sub":
        return "Submarine"
    return family


def _hardware(messages: list[tuple[float, str]]) -> tuple[str | None, str | None]:
    """(board name, board UID) from the boot banner."""
    for _, text in messages[:40]:
        m = BOARD_RE.match(text)
        if m and not FIRMWARE_RE.match(text):
            return m.group(1), m.group(2).replace(" ", "")
    return None, None


def _utc_start(log: DataFlashLog) -> datetime | None:
    gps = log.instances("GPS").get(0)
    if gps is None or not all(k in gps for k in ("GWk", "GMS", "Status")):
        return None
    ok = np.flatnonzero((gps["Status"] >= 3) & (gps["GWk"] > 0))
    if not ok.size:
        return None
    i = int(ok[0])
    try:
        utc = GPS_EPOCH + timedelta(
            weeks=int(gps["GWk"][i]),
            milliseconds=int(gps["GMS"][i]),
            seconds=-GPS_LEAP_SECONDS,
        )
        return utc - timedelta(seconds=float(log.time_s(gps)[i]))
    except (OverflowError, ValueError):  # a damaged GPS week or time of week
        return None


def _cells(
    signals: dict[str, Signal],
    quality: list[ParseIssue],
    params: dict[str, float],
    segments: list[Segment],
) -> None:
    """Cell count: ArduPilot has no standard parameter, so derive it carefully."""
    volt = signals.get("bat.volt")
    if volt is None or "bat.cells" in signals:
        return
    span = [float(volt.t[0]), float(volt.t[-1])]
    vmax = params.get("MOT_BAT_VOLT_MAX", params.get("Q_M_BAT_VOLT_MAX", 0.0))
    if vmax > 0:
        n = round(vmax / 4.2)
        if n > 0:
            signals["bat.cells"] = _step_signal(
                "bat.cells", "count", span, [n, n], f"MOT_BAT_VOLT_MAX {vmax:g} V / 4.2"
            )
            return
    # Resting voltage before the first arm (or the first second of the log).
    first_arm = segments[0].start if segments else float(volt.t[-1])
    rest = volt.values[
        (volt.t <= max(first_arm, float(volt.t[0]) + 1.0)) & np.isfinite(volt.values)
    ]
    if not rest.size:
        return
    v = float(np.median(rest))
    fits = [n for n in range(1, 15) if 3.6 <= v / n <= 4.25]
    if len(fits) == 1:
        n = fits[0]
        signals["bat.cells"] = _step_signal(
            "bat.cells", "count", span, [n, n], f"estimated from resting voltage {v:.2f} V"
        )
        quality.append(
            ParseIssue(
                "estimated",
                f"Battery cell count estimated as {n}S from the resting voltage ({v:.2f} V); "
                "set MOT_BAT_VOLT_MAX to make it exact",
            )
        )
    else:
        quality.append(
            ParseIssue(
                "invalid_values",
                f"Battery cell count is unknown: {v:.2f} V at rest fits "
                f"{' or '.join(f'{n}S' for n in fits) or 'no cell count'}",
            )
        )


def _dropouts(log: DataFlashLog, quality: list[ParseIssue]) -> None:
    dsf = log.messages.get("DSF")
    if dsf is None or "Dp" not in dsf or not dsf["Dp"].size:
        return
    dropped = int(dsf["Dp"].max())
    if dropped > 0:
        quality.append(ParseIssue("dropout", f"The logger dropped {dropped:,} message(s) (DSF.Dp)"))
