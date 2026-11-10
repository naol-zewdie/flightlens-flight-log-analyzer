"""PX4 ULog (.ulg) adapter.

Simple field-to-signal mappings live in ``mappings/px4.yaml``. Signals that need
computation are built here:

- ``att.roll/pitch/yaw`` and ``att.des_*`` (deg) from attitude quaternions
- ``vibe.metric`` (m/s²) and ``vibe.clip`` (cumulative count) from the IMU with
  the worst values, so a bad sensor is never hidden behind a good one
- ``mot.1..N`` (0-1) from ``actuator_motors``, or normalized PWM on older firmware
- armed segments, events (arming, modes, failsafes, logged messages), metadata

Time is seconds since the log's first timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pyulog import ULog

from flightlens.intervals import intervals_where
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

MAPPING_PATH = Path(__file__).parent / "mappings" / "px4.yaml"
RAD2DEG = 180.0 / np.pi
# vehicle_status.arming_state: 2 = ARMED in all versions; 3 = ARMED_ERROR (pre-1.9)
ARMED_STATES = (2, 3)

# vehicle_status.nav_state (NAVIGATION_STATE_*)
NAV_STATES: dict[int, str] = {
    0: "Manual",
    1: "Altitude",
    2: "Position",
    3: "Mission",
    4: "Hold",
    5: "Return",
    6: "Position slow",
    10: "Acro",
    12: "Descend",
    13: "Termination",
    14: "Offboard",
    15: "Stabilized",
    17: "Takeoff",
    18: "Land",
    19: "Follow target",
    20: "Precision land",
    21: "Orbit",
    22: "VTOL takeoff",
    **{23 + i: f"External {i + 1}" for i in range(8)},
}

# MAVLink MAV_TYPE (vehicle_status.system_type)
MAV_TYPES: dict[int, str] = {
    1: "Fixed wing",
    2: "Quadrotor",
    3: "Coaxial",
    4: "Helicopter",
    10: "Ground rover",
    12: "Submarine",
    13: "Hexarotor",
    14: "Octorotor",
    15: "Tricopter",
    19: "VTOL tailsitter",
    20: "VTOL tailsitter",
    21: "VTOL tiltrotor",
    22: "VTOL standard",
    23: "VTOL",
}
ROTOR_COUNT_BY_TYPE = {2: 4, 3: 2, 13: 6, 14: 8, 15: 3}

# vehicle_status.failure_detector_status bits (FAILURE_*)
FAILURE_BITS: dict[int, str] = {
    1: "roll angle exceeded",
    2: "pitch angle exceeded",
    4: "altitude loss",
    8: "external trigger",
    16: "ESC failure at arming",
    32: "battery failure",
    64: "imbalanced propeller",
    128: "motor failure",
}

# Topics whose absence the user should hear about.
EXPECTED_TOPICS: dict[str, tuple[str, ...]] = {
    "vehicle status": ("vehicle_status",),
    "attitude": ("vehicle_attitude",),
    "battery": ("battery_status",),
    "GPS": ("vehicle_gps_position", "sensor_gps"),
    "IMU status": ("vehicle_imu_status",),
    "motor outputs": ("actuator_motors", "actuator_outputs"),
    "estimator status": ("estimator_status",),
}

TopicKey = tuple[str, int]
TopicData = dict[str, Any]


class ULogParseError(Exception):
    pass


@lru_cache(maxsize=1)
def load_mapping() -> dict[str, Any]:
    with MAPPING_PATH.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def needed_topics(mapping: dict[str, Any]) -> list[str]:
    topics = {src["topic"] for spec in mapping["signals"] for src in spec["sources"]}
    topics.update(mapping.get("computed_topics", []))
    for names in EXPECTED_TOPICS.values():
        topics.update(names)
    return sorted(topics)


def parse(path: Path) -> Flight:
    mapping = load_mapping()
    try:
        ulog = ULog(str(path), needed_topics(mapping))
    except Exception as exc:  # pyulog raises a variety of errors on bad input
        raise ULogParseError(f"not a readable ULog file ({type(exc).__name__}: {exc})") from exc

    start_us = int(ulog.start_timestamp)
    topics: dict[TopicKey, TopicData] = {(d.name, int(d.multi_id)): d.data for d in ulog.data_list}
    quality: list[ParseIssue] = []
    times: dict[TopicKey, FloatArray] = {}

    def time_of(key: TopicKey) -> FloatArray:
        if key not in times:
            ts = topics[key]["timestamp"].astype(np.float64)
            t = np.maximum((ts - start_us) / 1e6, 0.0)
            if t.size > 1 and np.any(np.diff(t) < 0):
                quality.append(
                    ParseIssue("non_monotonic", f"{key[0]}[{key[1]}] timestamps go backwards")
                )
            times[key] = t
        return times[key]

    signals: dict[str, Signal] = {}

    def add(name: str, unit: str, key: TopicKey, values: FloatArray, source: str) -> None:
        t = time_of(key)
        if not np.any(np.isfinite(values)):
            quality.append(ParseIssue("invalid_values", f"{name}: no valid samples in {source}"))
            return
        order = np.argsort(t, kind="stable") if np.any(np.diff(t) < 0) else None
        if order is not None:
            t, values = t[order], values[order]
        signals[name] = Signal(name=name, unit=unit, t=t, values=values, source=source)

    # 1. Table-driven signals.
    for spec in mapping["signals"]:
        for src in spec["sources"]:
            key = (src["topic"], int(src.get("instance", 0)))
            data = topics.get(key)
            if data is None or src["field"] not in data:
                continue
            raw = data[src["field"]].astype(np.float64)
            if src.get("zero_is_missing"):
                raw = np.where(raw == 0, np.nan, raw)
            if "min_valid" in src:
                raw = np.where(raw < src["min_valid"], np.nan, raw)
            if "max_valid" in src:
                raw = np.where(raw > src["max_valid"], np.nan, raw)
            raw = raw * float(src.get("scale", 1.0))
            add(spec["name"], spec["unit"], key, raw, f"{key[0]}[{key[1]}].{src['field']}")
            break

    # 2. Computed signals.
    _attitude(topics, add)
    _vibration(topics, add, time_of, quality)
    params = {k: float(v) for k, v in ulog.initial_parameters.items()}
    system_type = _last_int(topics.get(("vehicle_status", 0)), "system_type")
    _motors(topics, add, params, system_type)

    segments, events = _status_events(topics, time_of)
    events += _logged_messages(ulog, start_us)
    events.sort(key=lambda e: e.t)

    # 3. Data quality.
    _dropouts(ulog, start_us, quality)
    for label, names in EXPECTED_TOPICS.items():
        if not any((n, 0) in topics for n in names):
            quality.append(ParseIssue("missing_topic", f"No {label} data ({' or '.join(names)})"))
    ends_armed = next((e for e in events if e.text == "Log ends while armed"), None)
    if ends_armed is not None:
        quality.append(
            ParseIssue(
                "ends_armed",
                "The log ends while the aircraft is still armed: the file may be truncated, "
                "or logging stopped in flight (power loss or crash)",
                ends_armed.t,
            )
        )
    if getattr(ulog, "file_corruption", False):
        quality.append(
            ParseIssue("corrupt", "The file is damaged or truncated; data after the damage is lost")
        )

    info = ulog.msg_info_dict
    metadata = FlightMetadata(
        log_format="ulog",
        source_path=path,
        vehicle_type=_vehicle_type(system_type, topics.get(("vehicle_status", 0))),
        firmware=_firmware(info),
        hardware=str(info["ver_hw"]) if "ver_hw" in info else None,
        start_time=_utc_start(topics, start_us),
        log_duration=(int(ulog.last_timestamp) - start_us) / 1e6,
        mapping_version=str(mapping["version"]),
    )
    status = topics.get(("vehicle_status", 0))
    arming_known = status is not None and "arming_state" in status
    if arming_known and not segments:
        quality.append(
            ParseIssue("never_armed", "The aircraft never armed in this log, so there is no flight")
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


# --- computed signals -------------------------------------------------------


def _quat_to_euler(
    w: FloatArray, x: FloatArray, y: FloatArray, z: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray]:
    roll = np.arctan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = np.arcsin(np.clip(2 * (w * y - z * x), -1.0, 1.0))
    yaw = np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return roll * RAD2DEG, pitch * RAD2DEG, yaw * RAD2DEG


def _attitude(topics: dict[TopicKey, TopicData], add: Any) -> None:
    key = ("vehicle_attitude", 0)
    d = topics.get(key)
    if d is not None and "q[0]" in d:
        q = [d[f"q[{i}]"].astype(np.float64) for i in range(4)]
        for axis, vals in zip(("roll", "pitch", "yaw"), _quat_to_euler(*q), strict=True):
            add(f"att.{axis}", "deg", key, vals, "vehicle_attitude[0].q")

    key = ("vehicle_attitude_setpoint", 0)
    d = topics.get(key)
    if d is None:
        return
    if "q_d[0]" in d:
        q = [d[f"q_d[{i}]"].astype(np.float64) for i in range(4)]
        for axis, vals in zip(("roll", "pitch", "yaw"), _quat_to_euler(*q), strict=True):
            add(f"att.des_{axis}", "deg", key, vals, "vehicle_attitude_setpoint[0].q_d")
    else:
        for axis in ("roll", "pitch", "yaw"):
            f = f"{axis}_body"
            if f in d:
                vals = d[f].astype(np.float64) * RAD2DEG
                add(f"att.des_{axis}", "deg", key, vals, f"vehicle_attitude_setpoint[0].{f}")


def _vibration(
    topics: dict[TopicKey, TopicData], add: Any, time_of: Any, quality: list[ParseIssue]
) -> None:
    imus = sorted(k for k in topics if k[0] == "vehicle_imu_status")
    best_metric: tuple[float, TopicKey] | None = None
    best_clip: tuple[float, TopicKey] | None = None
    saw_metric = False
    for key in imus:
        d = topics[key]
        if "accel_vibration_metric" in d:
            saw_metric = True
            m = d["accel_vibration_metric"].astype(np.float64)
            finite = m[np.isfinite(m)]
            if finite.size and finite.max() > 0:
                score = float(np.percentile(finite, 95))
                if best_metric is None or score > best_metric[0]:
                    best_metric = (score, key)
        clip_fields = [f"accel_clipping[{i}]" for i in range(3) if f"accel_clipping[{i}]" in d]
        if clip_fields:
            total = sum(d[f].astype(np.float64) for f in clip_fields)
            growth = float(total[-1] - total[0]) if total.size else 0.0
            if best_clip is None or growth > best_clip[0]:
                best_clip = (growth, key)

    if best_metric is not None:
        key = best_metric[1]
        add(
            "vibe.metric",
            "m/s²",
            key,
            topics[key]["accel_vibration_metric"].astype(np.float64),
            f"{key[0]}[{key[1]}].accel_vibration_metric (worst of {len(imus)} IMU)",
        )
    elif saw_metric:
        quality.append(
            ParseIssue(
                "invalid_values",
                "accel_vibration_metric is zero on every IMU (not computed by this firmware)",
            )
        )
    if best_clip is not None:
        key = best_clip[1]
        d = topics[key]
        total = sum(
            d[f"accel_clipping[{i}]"].astype(np.float64)
            for i in range(3)
            if f"accel_clipping[{i}]" in d
        )
        add(
            "vibe.clip",
            "count",
            key,
            np.asarray(total, dtype=np.float64),
            f"{key[0]}[{key[1]}].accel_clipping (worst of {len(imus)} IMU)",
        )


def _motors(
    topics: dict[TopicKey, TopicData],
    add: Any,
    params: dict[str, float],
    system_type: int | None,
) -> None:
    rotor_count = int(params.get("CA_ROTOR_COUNT", 0)) or ROTOR_COUNT_BY_TYPE.get(
        system_type or -1, 0
    )

    key = ("actuator_motors", 0)
    d = topics.get(key)
    if d is not None:
        n = rotor_count or 12
        lift = pusher = 0
        for i in range(n):
            f = f"control[{i}]"
            if f not in d:
                break
            vals = d[f].astype(np.float64)
            if not np.any(np.isfinite(vals)):
                continue
            # Control allocation geometry: a rotor whose thrust axis is mostly
            # horizontal (|AZ| < 0.5) is a pusher/puller, not a lift motor.
            az = params.get(f"CA_ROTOR{i}_AZ")
            if az is not None and abs(az) < 0.5:
                pusher += 1
                add(f"pusher.{pusher}", "fraction", key, vals, f"actuator_motors[0].{f}")
            else:
                lift += 1
                add(f"mot.{lift}", "fraction", key, vals, f"actuator_motors[0].{f}")
        return

    key = ("actuator_outputs", 0)
    d = topics.get(key)
    if d is None or rotor_count == 0:
        return
    lo = params.get("PWM_MAIN_MIN", params.get("PWM_MIN", 1000.0))
    hi = params.get("PWM_MAIN_MAX", params.get("PWM_MAX", 2000.0))
    if hi <= lo:
        return
    for i in range(rotor_count):
        f = f"output[{i}]"
        if f not in d:
            break
        vals = (d[f].astype(np.float64) - lo) / (hi - lo)
        add(f"mot.{i + 1}", "fraction", key, vals, f"actuator_outputs[0].{f} (PWM {lo:g}-{hi:g})")


# --- segments and events ----------------------------------------------------


def _status_events(
    topics: dict[TopicKey, TopicData], time_of: Any
) -> tuple[list[Segment], list[Event]]:
    key = ("vehicle_status", 0)
    d = topics.get(key)
    if d is None:
        return [], []
    t = time_of(key)
    events: list[Event] = []
    segments: list[Segment] = []

    if "arming_state" in d:
        armed = np.isin(d["arming_state"].astype(np.int64), ARMED_STATES)
        for a, b in intervals_where(t, armed):
            segments.append(Segment(a, b))
            events.append(Event(a, EventKind.ARM, "Armed", source="vehicle_status.arming_state"))
            if b < t[-1]:
                events.append(
                    Event(b, EventKind.DISARM, "Disarmed", source="vehicle_status.arming_state")
                )
        if armed.size and armed[-1]:
            events.append(
                Event(
                    float(t[-1]),
                    EventKind.ERROR,
                    "Log ends while armed",
                    level="warning",
                    source="vehicle_status.arming_state",
                )
            )

    if "nav_state" in d:
        nav = d["nav_state"].astype(np.int64)
        changes = np.flatnonzero(np.r_[True, nav[1:] != nav[:-1]])
        for i in changes:
            name = NAV_STATES.get(int(nav[i]), f"Mode {int(nav[i])}")
            events.append(
                Event(float(t[i]), EventKind.MODE, name, source="vehicle_status.nav_state")
            )

    if "failsafe" in d:
        fs = d["failsafe"].astype(bool)
        rising = np.flatnonzero(np.r_[fs[0], fs[1:] & ~fs[:-1]])
        for i in rising:
            events.append(
                Event(
                    float(t[i]),
                    EventKind.FAILSAFE,
                    "Failsafe triggered",
                    level="critical",
                    source="vehicle_status.failsafe",
                )
            )

    if "failure_detector_status" in d:
        fd = d["failure_detector_status"].astype(np.int64)
        prev = np.r_[0, fd[:-1]]
        for i in np.flatnonzero(fd & ~prev):
            new_bits = int(fd[i] & ~prev[i])
            names = [label for bit, label in FAILURE_BITS.items() if new_bits & bit]
            events.append(
                Event(
                    float(t[i]),
                    EventKind.FAILSAFE,
                    f"Failure detector: {', '.join(names) or f'code {new_bits}'}",
                    level="critical",
                    source="vehicle_status.failure_detector_status",
                )
            )
    return segments, events


def _logged_messages(ulog: ULog, start_us: int) -> list[Event]:
    events = []
    for m in ulog.logged_messages:
        level = int(m.log_level)
        if level <= 2:
            lvl, kind = "critical", EventKind.ERROR
        elif level == 3:
            lvl, kind = "error", EventKind.ERROR
        elif level == 4:
            lvl, kind = "warning", EventKind.MESSAGE
        else:
            lvl, kind = "info", EventKind.MESSAGE
        text = " ".join(str(m.message).split())  # collapse tabs and newlines
        t = max((int(m.timestamp) - start_us) / 1e6, 0.0)
        events.append(Event(t, kind, text, lvl, "logged message"))
    return events


# --- metadata and quality ---------------------------------------------------


def _dropouts(ulog: ULog, start_us: int, quality: list[ParseIssue]) -> None:
    drops = list(ulog.dropouts)
    if not drops:
        return
    total_ms = sum(int(d.duration) for d in drops)
    worst = max(drops, key=lambda d: int(d.duration))
    quality.append(
        ParseIssue(
            "dropout",
            f"{len(drops)} logging dropout(s), {total_ms} ms lost in total "
            f"(longest {int(worst.duration)} ms)",
            (int(worst.timestamp) - start_us) / 1e6,
        )
    )


def _firmware(info: dict[str, Any]) -> str | None:
    rel = info.get("ver_sw_release")
    git = str(info.get("ver_sw", ""))[:7]
    if rel is None:
        return f"PX4 {git}" if git else None
    rel = int(rel)
    major, minor, patch, kind = (
        (rel >> 24) & 0xFF,
        (rel >> 16) & 0xFF,
        (rel >> 8) & 0xFF,
        rel & 0xFF,
    )
    text = f"PX4 v{major}.{minor}.{patch}"
    if kind < 255:
        text += " (dev)" if kind < 64 else " (pre-release)"
    return f"{text} {git}".strip() if git else text


def _last_int(data: TopicData | None, field: str) -> int | None:
    if data is None or field not in data or not len(data[field]):
        return None
    return int(data[field][-1])


def _vehicle_type(system_type: int | None, status: TopicData | None) -> str | None:
    if system_type is None:
        return None
    name = MAV_TYPES.get(system_type)
    if name is None and status is not None and "is_vtol" in status and bool(status["is_vtol"][-1]):
        return "VTOL"
    return name


def _utc_start(topics: dict[TopicKey, TopicData], start_us: int) -> datetime | None:
    for name in ("vehicle_gps_position", "sensor_gps"):
        d = topics.get((name, 0))
        if d is None or "time_utc_usec" not in d:
            continue
        utc = d["time_utc_usec"].astype(np.int64)
        valid = np.flatnonzero(utc > 1_000_000_000_000_000)  # after 2001
        if valid.size:
            i = valid[0]
            start = (int(utc[i]) - (int(d["timestamp"][i]) - start_us)) / 1e6
            return datetime.fromtimestamp(start, tz=UTC)
    return None
