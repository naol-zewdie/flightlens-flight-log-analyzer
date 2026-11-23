"""MAVLink telemetry log (.tlog) adapter.

Field-to-signal mappings live in ``mappings/mavlink.yaml``. Built here:

- which sender is the vehicle: the system whose autopilot component sends the
  most HEARTBEATs (a tlog also holds the ground station, gimbals, companions);
- armed segments, flight modes and attitude control from HEARTBEAT
  (``base_mode`` armed flag, ``custom_mode`` per ArduPilot vehicle or PX4);
- landed state from EXTENDED_SYS_STATE, clipping as the sum of VIBRATION's
  three counters, motors from SERVO_OUTPUT_RAW (ArduPilot ``SERVOn_FUNCTION``);
- parameters from PARAM_VALUE (the ground station downloads them on connect),
  messages and failsafes from STATUSTEXT, firmware and board id from
  AUTOPILOT_VERSION and the boot banner;
- PX4 attitude setpoint from ATTITUDE_TARGET's quaternion.

Time is the ground station's receive time, in seconds since the vehicle's first
packet. Packets lost on the radio link (gaps in the sequence numbers) are
reported as a data-quality issue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from flightlens.parsers import mavlink
from flightlens.parsers._mapping import TopicKey, apply_mapping, arming_quality, load_mapping
from flightlens.parsers.ardupilot import (
    BOARD_RE,
    FAMILIES,
    FIRMWARE_RE,
    MODES_BY_FAMILY,
    MOTOR_FUNCTIONS,
    NO_ATTITUDE_CONTROL,
    THROTTLE_FUNCTION,
    _cells,
    _pwm_range,
)
from flightlens.parsers.mavlink import TlogParseError
from flightlens.parsers.ulog import MAV_TYPES, ROTOR_COUNT_BY_TYPE

__all__ = ["TlogParseError", "parse"]

AUTOPILOT_ARDUPILOT = 3
AUTOPILOT_PX4 = 12
AUTOPILOT_INVALID = 8
GCS_TYPES = {6, 18, 26, 27}  # ground station, onboard controller, gimbal, ADS-B
ARMED_FLAG = 128
LANDED_STATE_ON_GROUND = 1
RAD2DEG = 180.0 / np.pi

# PX4 custom_mode: main mode in bits 16-23, sub mode in bits 24-31
PX4_MAIN_MODES = {
    1: "Manual",
    2: "Altitude",
    3: "Position",
    4: "Auto",
    5: "Acro",
    6: "Offboard",
    7: "Stabilized",
    8: "Rattitude",
}
PX4_AUTO_MODES = {
    1: "Ready",
    2: "Takeoff",
    3: "Hold",
    4: "Mission",
    5: "Return",
    6: "Land",
    8: "Follow target",
    9: "Precision land",
}
PX4_NO_ATTITUDE_CONTROL = {1, 5}  # Manual and Acro command rates or surfaces

# family of an ArduPilot vehicle by MAV_TYPE (the mode table depends on it)
ARDUPILOT_FAMILY = {
    1: "Plane",
    2: "Copter",
    3: "Copter",
    4: "Copter",
    13: "Copter",
    14: "Copter",
    15: "Copter",
    10: "Rover",
    11: "Rover",
    12: "Sub",
    **{t: "Plane" for t in (19, 20, 21, 22, 23)},  # QuadPlanes report VTOL types
}
FAILSAFE_WORDS = ("failsafe", "fail-safe", "crash", "lost", "thrust loss")


def parse(path: Path) -> Flight:
    mapping = load_mapping("mavlink")
    raw = mavlink.read(path)
    sysid, compid = _vehicle(raw)
    quality: list[ParseIssue] = []

    t_first = min(
        (s.t[0] for (sy, co, _), s in raw.streams.items() if (sy, co) == (sysid, compid) and s.t),
        default=None,
    )
    if t_first is None:
        raise TlogParseError("the log holds no messages from the vehicle")

    cache: dict[str, tuple[FloatArray, dict[str, Any]]] = {}

    def msg(name: str) -> tuple[FloatArray, dict[str, Any]]:
        if name not in cache:
            t, cols = raw.columns(sysid, compid, name)
            cache[name] = (t - t_first, cols)
        return cache[name]

    signals: dict[str, Signal] = {}

    def add(name: str, unit: str, key: TopicKey, values: FloatArray, source: str) -> None:
        t, _ = msg(key[0])
        _put(signals, quality, name, unit, t, values, source)

    def lookup(topic: str, instance: int, field: str) -> FloatArray | None:
        if instance != 0 or topic not in mavlink.BY_NAME:
            return None
        t, cols = msg(topic)
        if field not in cols or not t.size:
            return None
        return np.asarray(cols[field], dtype=np.float64)

    hb_t, hb = msg("HEARTBEAT")
    mav_type = int(np.bincount(hb["type"].astype(np.int64)).argmax())
    autopilot = int(np.bincount(hb["autopilot"].astype(np.int64)).argmax())
    messages = _statustexts(*msg("STATUSTEXT"))
    params = _parameters(*msg("PARAM_VALUE"))
    firmware, family = _firmware(autopilot, mav_type, messages, *msg("AUTOPILOT_VERSION"))

    # 1. Table-driven signals.
    apply_mapping(mapping, lookup, add)

    # 2. Computed signals.
    _clipping(signals, quality, *msg("VIBRATION"))
    _landed(signals, *msg("EXTENDED_SYS_STATE"))
    if autopilot == AUTOPILOT_PX4:
        _px4_attitude_setpoint(signals, quality, *msg("ATTITUDE_TARGET"))
        _px4_motors(signals, quality, mav_type, *msg("SERVO_OUTPUT_RAW"))
    else:
        _ardupilot_motors(signals, quality, params, *msg("SERVO_OUTPUT_RAW"))
    segments, arm_events = _arming(hb_t, hb)
    mode_events, att_ctrl = _modes(hb_t, hb, autopilot, family)
    if att_ctrl is not None:
        signals["state.att_ctrl"] = att_ctrl
    _cells(signals, quality, params, segments)

    events = arm_events + mode_events + [_message_event(t, sev, text) for t, sev, text in messages]
    events.sort(key=lambda e: e.t)

    # 3. Data quality.
    _link_quality(raw, sysid, compid, quality)
    for label, names in (
        ("attitude", ("ATTITUDE",)),
        ("position", ("GLOBAL_POSITION_INT",)),
        ("battery", ("SYS_STATUS",)),
        ("parameters", ("PARAM_VALUE",)),
    ):
        if not any(msg(n)[0].size for n in names):
            quality.append(
                ParseIssue("missing_topic", f"No {label} data ({' or '.join(names)} messages)")
            )
    arming_quality(events, segments, True, quality)

    end = max((float(s.t[-1]) for s in signals.values() if len(s)), default=float(hb_t[-1]))
    hardware, serial = _board(messages, *msg("AUTOPILOT_VERSION"))
    vehicle = MAV_TYPES.get(mav_type)
    if family == "Plane" and params.get("Q_ENABLE", 0) > 0:
        vehicle = "VTOL (QuadPlane)"
    metadata = FlightMetadata(
        log_format="tlog",
        source_path=path,
        vehicle_type=vehicle,
        firmware=firmware,
        hardware=hardware,
        serial=serial,
        start_time=_start_time(t_first, *msg("SYSTEM_TIME")),
        log_duration=end,
        mapping_version=str(mapping["version"]),
    )
    return Flight(
        metadata=metadata,
        signals=signals,
        events=events,
        parameters=params,
        segments=segments,
        quality=quality,
        arming_known=True,
    )


# --- helpers ----------------------------------------------------------------------------


def _vehicle(raw: mavlink.TlogData) -> tuple[int, int]:
    """The sender that is the vehicle's autopilot."""
    votes: dict[tuple[int, int], int] = {}
    for (sysid, compid, name), s in raw.streams.items():
        if name != "HEARTBEAT":
            continue
        types = {int(r[0]) for r in _rows_as_dict(s.rows, "HEARTBEAT", "type")}
        pilots = {int(r[0]) for r in _rows_as_dict(s.rows, "HEARTBEAT", "autopilot")}
        if types & GCS_TYPES or pilots == {AUTOPILOT_INVALID}:
            continue
        votes[(sysid, compid)] = len(s.rows)
    if not votes:
        raise TlogParseError(
            "no vehicle in this telemetry log (only ground station or peripheral heartbeats)"
        )
    return max(votes, key=lambda k: votes[k])


def _rows_as_dict(rows: list[tuple[Any, ...]], msg: str, field: str) -> list[tuple[Any]]:
    i = mavlink.BY_NAME[msg].columns().index(field)
    return [(r[i],) for r in rows]


def _put(
    signals: dict[str, Signal],
    quality: list[ParseIssue],
    name: str,
    unit: str,
    t: FloatArray,
    values: FloatArray,
    source: str,
) -> None:
    if not t.size or not np.any(np.isfinite(values)):
        quality.append(ParseIssue("invalid_values", f"{name}: no valid samples in {source}"))
        return
    if np.any(np.diff(t) < 0):
        order = np.argsort(t, kind="stable")
        t, values = t[order], values[order]
    signals[name] = Signal(name=name, unit=unit, t=t, values=values, source=source)


def _step(name: str, unit: str, t: list[float], v: list[float], source: str) -> Signal:
    return Signal(
        name, unit, np.asarray(t, dtype=np.float64), np.asarray(v, dtype=np.float64), source
    )


def _text(raw: Any) -> str:
    b = raw if isinstance(raw, bytes) else bytes(raw)
    return b.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def _statustexts(t: FloatArray, cols: dict[str, Any]) -> list[tuple[float, int, str]]:
    if not t.size:
        return []
    return [
        (float(ti), int(sev), " ".join(_text(txt).split()))
        for ti, sev, txt in zip(t, cols["severity"], cols["text"], strict=True)
        if _text(txt)
    ]


def _message_event(t: float, severity: int, text: str) -> Event:
    lowered = text.lower()
    if text.startswith(("PreArm", "Arm:")) or "preflight fail" in lowered:
        return Event(t, EventKind.MESSAGE, text, "warning", "STATUSTEXT")
    if severity <= 3 and any(w in lowered for w in FAILSAFE_WORDS):
        return Event(t, EventKind.FAILSAFE, text, "error", "STATUSTEXT")
    if severity <= 2:
        return Event(t, EventKind.ERROR, text, "critical", "STATUSTEXT")
    if severity == 3:
        return Event(t, EventKind.ERROR, text, "error", "STATUSTEXT")
    level = "warning" if severity == 4 else "info"
    return Event(t, EventKind.MESSAGE, text, level, "STATUSTEXT")


def _parameters(t: FloatArray, cols: dict[str, Any]) -> dict[str, float]:
    params: dict[str, float] = {}
    if not t.size:
        return params
    for name, value in zip(cols["param_id"], cols["param_value"], strict=True):
        key = _text(name)
        if key and np.isfinite(value):
            params.setdefault(key, float(value))  # the first download describes this flight
    return params


def _firmware(
    autopilot: int,
    mav_type: int,
    messages: list[tuple[float, int, str]],
    t: FloatArray,
    version: dict[str, Any],
) -> tuple[str | None, str | None]:
    if autopilot == AUTOPILOT_ARDUPILOT:
        for _, _, text in messages[:80]:
            m = FIRMWARE_RE.match(text)
            if m:
                vehicle, ver, git = m.groups()
                fw = f"{vehicle} V{ver.lstrip('Vv')}" + (f" {git[:8]}" if git else "")
                return fw, FAMILIES.get(vehicle)
        family = ARDUPILOT_FAMILY.get(mav_type)
        name = {"Plane": "ArduPlane", "Copter": "ArduCopter", "Rover": "ArduRover"}.get(
            family or "", "ArduPilot"
        )
        return (_version_text(name, t, version) or name), family
    if autopilot == AUTOPILOT_PX4:
        return _version_text("PX4", t, version) or "PX4", None
    # e.g. Betaflight and INAV report a generic autopilot over MAVLink
    return _version_text("Generic MAVLink autopilot", t, version), None


def _version_text(name: str, t: FloatArray, version: dict[str, Any]) -> str | None:
    if not t.size:
        return None
    v = int(version["flight_sw_version"][-1])
    if not v:
        return None
    return f"{name} V{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"


def _board(
    messages: list[tuple[float, int, str]], t: FloatArray, version: dict[str, Any]
) -> tuple[str | None, str | None]:
    for _, _, text in messages[:80]:
        m = BOARD_RE.match(text)
        if m and not FIRMWARE_RE.match(text):
            return m.group(1), m.group(2).replace(" ", "")
    if t.size:
        uid = int(version["uid"][-1])
        uid2 = bytes(int(version[f"uid2[{i}]"][-1]) for i in range(18))
        if any(uid2):
            return None, uid2.hex().upper()
        if uid:
            return None, f"{uid:016X}"
    return None, None


def _start_time(t_first: float, t: FloatArray, cols: dict[str, Any]) -> datetime | None:
    """The vehicle's clock (GPS) when it has one, else the ground station's."""
    if t.size:
        unix = cols["time_unix_usec"]
        ok = np.flatnonzero(unix > mavlink.MIN_UNIX_US)
        if ok.size:
            i = int(ok[0])
            start = unix[i] / 1e6 - float(t[i])
            try:
                return datetime.fromtimestamp(start, tz=UTC)
            except (OverflowError, OSError, ValueError):
                pass
    try:
        return datetime.fromtimestamp(t_first, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _clipping(
    signals: dict[str, Signal], quality: list[ParseIssue], t: FloatArray, cols: dict[str, Any]
) -> None:
    if not t.size:
        return
    total = cols["clipping_0"] + cols["clipping_1"] + cols["clipping_2"]
    _put(signals, quality, "vibe.clip", "count", t, total, "VIBRATION.clipping_0+1+2")


def _landed(signals: dict[str, Signal], t: FloatArray, cols: dict[str, Any]) -> None:
    if not t.size:
        return
    state = cols["landed_state"]
    known = state > 0  # 0: undefined
    if known.sum() < 2:
        return
    landed = (state[known] == LANDED_STATE_ON_GROUND).astype(np.float64)
    signals["state.landed"] = Signal(
        "state.landed", "flag", t[known], landed, "EXTENDED_SYS_STATE.landed_state"
    )


def _px4_attitude_setpoint(
    signals: dict[str, Signal], quality: list[ParseIssue], t: FloatArray, cols: dict[str, Any]
) -> None:
    if not t.size:
        return
    w, x, y, z = (cols[f"q[{i}]"] for i in range(4))
    norm = np.sqrt(w * w + x * x + y * y + z * z)
    ok = norm > 0.5
    roll = np.degrees(np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
    pitch = np.degrees(np.arcsin(np.clip(2 * (w * y - z * x), -1, 1)))
    _put(signals, quality, "att.des_roll", "deg", t[ok], roll[ok], "ATTITUDE_TARGET.q")
    _put(signals, quality, "att.des_pitch", "deg", t[ok], pitch[ok], "ATTITUDE_TARGET.q")
    for axis in ("roll", "pitch", "yaw"):
        rate = cols[f"body_{axis}_rate"] * RAD2DEG
        _put(
            signals,
            quality,
            f"rate.des_{axis}",
            "deg/s",
            t,
            rate,
            f"ATTITUDE_TARGET.body_{axis}_rate",
        )


def _ardupilot_motors(
    signals: dict[str, Signal],
    quality: list[ParseIssue],
    params: dict[str, float],
    t: FloatArray,
    cols: dict[str, Any],
) -> None:
    if not t.size:
        return
    port = cols["port"] == 0
    motors: dict[int, int] = {}
    throttle = None
    for ch in range(1, 17):
        fn = int(params.get(f"SERVO{ch}_FUNCTION", -1))
        if fn in MOTOR_FUNCTIONS:
            motors.setdefault(MOTOR_FUNCTIONS[fn], ch)
        elif fn == THROTTLE_FUNCTION and throttle is None:
            throttle = ch
    lo, hi = _pwm_range(params)
    for i, (motor, ch) in enumerate(sorted(motors.items()), start=1):
        pwm = cols[f"servo{ch}_raw"][port]
        vals = np.where(pwm > 0, (pwm - lo) / (hi - lo), np.nan)
        _put(
            signals, quality, f"mot.{i}", "fraction", t[port], vals,
            f"SERVO_OUTPUT_RAW.servo{ch}_raw (motor {motor}, PWM {lo:g}-{hi:g})",
        )  # fmt: skip
    if throttle is not None:
        tlo = params.get(f"SERVO{throttle}_MIN", 1000.0)
        thi = params.get(f"SERVO{throttle}_MAX", 2000.0)
        pwm = cols[f"servo{throttle}_raw"][port]
        if thi > tlo:
            vals = np.where(pwm > 0, (pwm - tlo) / (thi - tlo), np.nan)
            _put(
                signals, quality, "pusher.1", "fraction", t[port], vals,
                f"SERVO_OUTPUT_RAW.servo{throttle}_raw (throttle)",
            )  # fmt: skip


def _px4_motors(
    signals: dict[str, Signal],
    quality: list[ParseIssue],
    mav_type: int,
    t: FloatArray,
    cols: dict[str, Any],
) -> None:
    n = ROTOR_COUNT_BY_TYPE.get(mav_type)
    if not t.size or n is None:
        return
    port = cols["port"] == 0
    for i in range(1, n + 1):
        pwm = cols[f"servo{i}_raw"][port]
        vals = np.where(pwm > 0, (pwm - 1000.0) / 1000.0, np.nan)
        _put(
            signals, quality, f"mot.{i}", "fraction", t[port], vals,
            f"SERVO_OUTPUT_RAW.servo{i}_raw (PWM 1000-2000)",
        )  # fmt: skip


def _arming(t: FloatArray, hb: dict[str, Any]) -> tuple[list[Segment], list[Event]]:
    armed = (hb["base_mode"].astype(np.int64) & ARMED_FLAG) > 0
    segments, events = [], []
    start = None
    for ti, a in zip(t.tolist(), armed.tolist(), strict=True):
        if a and start is None:
            start = ti
            events.append(Event(ti, EventKind.ARM, "Armed", source="HEARTBEAT.base_mode"))
        elif not a and start is not None:
            segments.append(Segment(start, ti))
            events.append(Event(ti, EventKind.DISARM, "Disarmed", source="HEARTBEAT.base_mode"))
            start = None
    if start is not None:
        end = float(t[-1])
        segments.append(Segment(start, end))
        events.append(Event(end, EventKind.MESSAGE, "Log ends while armed", "warning", "HEARTBEAT"))
    return segments, events


def _mode_name(autopilot: int, family: str | None, custom: int) -> tuple[str, bool]:
    """Mode name, and whether it stabilises attitude."""
    if autopilot == AUTOPILOT_PX4:
        main, sub = (custom >> 16) & 0xFF, (custom >> 24) & 0xFF
        name = PX4_MAIN_MODES.get(main, f"Mode {main}")
        if main == 4:
            name = PX4_AUTO_MODES.get(sub, "Auto")
        return name, main not in PX4_NO_ATTITUDE_CONTROL
    names = MODES_BY_FAMILY.get(family or "", {})
    no_att = NO_ATTITUDE_CONTROL.get(family or "", set())
    return names.get(custom, f"Mode {custom}"), custom not in no_att


def _modes(
    t: FloatArray, hb: dict[str, Any], autopilot: int, family: str | None
) -> tuple[list[Event], Signal | None]:
    events: list[Event] = []
    times: list[float] = []
    flags: list[float] = []
    last = None
    for ti, custom in zip(t.tolist(), hb["custom_mode"].astype(np.int64).tolist(), strict=True):
        if custom == last:
            continue
        last = custom
        name, att = _mode_name(autopilot, family, custom)
        events.append(Event(ti, EventKind.MODE, name, source="HEARTBEAT.custom_mode"))
        times.append(ti)
        flags.append(float(att))
    if not times or (autopilot != AUTOPILOT_PX4 and family not in NO_ATTITUDE_CONTROL):
        return events, None
    att_ctrl = _step(
        "state.att_ctrl",
        "flag",
        [0.0, *times],
        [flags[0], *flags],
        "HEARTBEAT.custom_mode (attitude-controlled modes)",
    )
    return events, att_ctrl


def _link_quality(
    raw: mavlink.TlogData, sysid: int, compid: int, quality: list[ParseIssue]
) -> None:
    key = (sysid, compid)
    got, lost = raw.received.get(key, 0), raw.lost.get(key, 0)
    if got and lost / (got + lost) > 0.01:
        quality.append(
            ParseIssue(
                "dropout",
                f"The telemetry link lost {lost / (got + lost):.0%} of the vehicle's packets "
                f"({lost:,} of {got + lost:,}); gaps in the signals are radio dropouts",
            )
        )
    if raw.crc_errors or raw.bytes_skipped:
        quality.append(
            ParseIssue(
                "corrupt",
                f"{raw.crc_errors:,} damaged packet(s) and {raw.bytes_skipped:,} unreadable "
                "byte(s) were skipped",
            )
        )
    quality.append(
        ParseIssue(
            "low_rate",
            "Telemetry log: what the ground station received over the radio, usually 1-10 "
            "samples per second. Checks that need the onboard log's high-rate data (noise "
            "spectrum, rate-loop tuning) cannot run; analyse the .bin or .ulg for those",
        )
    )
