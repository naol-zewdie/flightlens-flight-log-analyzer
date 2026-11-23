"""Telemetry logs (.tlog) from simulated flights: what a ground station would record.

``telemetry(flight, path)`` re-sends a flight (usually one parsed from a
generated DataFlash log) as an ArduPilot vehicle streams it over a telemetry
radio: each MAVLink message at its usual rate (1-10 Hz), the parameter download
when the ground station connects, status texts for messages and failsafes, a
ground-station heartbeat in between, and optionally packets lost on the link.
Every fault the DataFlash simulator models therefore also exists as a tlog.
"""

from __future__ import annotations

import random
import struct
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from flightlens.model import EventKind, Flight, Signal
from flightlens.parsers.ardupilot import MODES_BY_FAMILY, MOTOR_FUNCTIONS, _pwm_range
from flightlens.parsers.mavlink import BY_NAME, STX_V1, STX_V2, MessageDef, x25

VEHICLE = (1, 1)  # system, component
GCS = (255, 190)
MAV_TYPE = {"Quadrotor": 2, "Hexarotor": 13, "Octorotor": 14, "Fixed wing": 1}
DEFAULT_START = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)

# message -> rate (Hz), roughly ArduPilot's default stream rates over a radio
RATES = {
    "ATTITUDE": 10.0,
    "NAV_CONTROLLER_OUTPUT": 5.0,
    "GLOBAL_POSITION_INT": 5.0,
    "VFR_HUD": 5.0,
    "RAW_IMU": 5.0,
    "SERVO_OUTPUT_RAW": 5.0,
    "GPS_RAW_INT": 2.0,
    "SYS_STATUS": 2.0,
    "VIBRATION": 2.0,
    "EKF_STATUS_REPORT": 2.0,
    "RC_CHANNELS": 2.0,
    "BATTERY_STATUS": 1.0,
    "POWER_STATUS": 1.0,
    "EXTENDED_SYS_STATE": 1.0,
    "SYSTEM_TIME": 1.0,
}


class TlogWriter:
    """Encodes MAVLink frames, each prefixed by an 8-byte big-endian timestamp."""

    def __init__(self, v2: bool = True) -> None:
        self.v2 = v2
        self.buf = bytearray()
        self.seq: dict[tuple[int, int], int] = {}

    def frame(self, sender: tuple[int, int], name: str, fields: dict[str, Any]) -> bytes:
        msg = BY_NAME[name]
        payload = _pack(msg, fields)
        seq = self.seq.get(sender, 0)
        self.seq[sender] = (seq + 1) % 256
        sysid, compid = sender
        if self.v2:
            body = payload.rstrip(b"\0") or payload[:1]  # MAVLink 2 drops trailing zeros
            header = bytes(
                [
                    len(body),
                    0,
                    0,
                    seq,
                    sysid,
                    compid,
                    msg.id & 0xFF,
                    msg.id >> 8 & 0xFF,
                    msg.id >> 16,
                ]
            )
            stx = STX_V2
        else:
            body = payload[: _base_length(msg)]
            header = bytes([len(body), seq, sysid, compid, msg.id])
            stx = STX_V1
        crc = x25(bytes([msg.crc_extra]), x25(header + body))
        return bytes([stx]) + header + body + struct.pack("<H", crc)

    def write(self, t_us: int, sender: tuple[int, int], name: str, **fields: Any) -> bytes:
        rec = t_us.to_bytes(8, "big") + self.frame(sender, name, fields)
        self.buf += rec
        return rec

    def save(self, path: Path) -> Path:
        path.write_bytes(bytes(self.buf))
        return path


def _base_length(msg: MessageDef) -> int:
    return sum(f.size for f in msg.fields[: msg.n_base])


def _pack(msg: MessageDef, fields: dict[str, Any]) -> bytes:
    values: list[Any] = []
    for f in msg.fields:
        v = fields.get(f.name, 0)
        if f.base == "char":
            values.append(v.encode() if isinstance(v, str) else bytes(v or b""))
        elif f.length:
            arr = list(v) if v else []
            values += [_cast(f.base, x) for x in (arr + [0] * f.length)[: f.length]]
        else:
            values.append(_cast(f.base, v))
    return msg.struct.pack(*values)


def _cast(ctype: str, v: Any) -> Any:
    if ctype in ("float", "double"):
        return float(v)
    bits = {"uint8_t": 8, "uint16_t": 16, "uint32_t": 32, "uint64_t": 64}
    if ctype in bits:
        return max(0, min(int(np.rint(v)), 2 ** bits[ctype] - 1))
    sbits = {"int8_t": 8, "int16_t": 16, "int32_t": 32, "int64_t": 64}
    lim = 2 ** (sbits[ctype] - 1)
    return max(-lim, min(int(np.rint(v)), lim - 1))


# --- a flight as a telemetry stream ---------------------------------------------------


def _at(flight: Flight, name: str, t: float, default: float = 0.0) -> float:
    s: Signal | None = flight.signals.get(name)
    if s is None or not len(s):
        return default
    ok = np.isfinite(s.values)
    if not ok.any():
        return default
    i = int(np.searchsorted(s.t[ok], t, side="right")) - 1
    if i < 0:
        return float(s.values[ok][0])
    return float(s.values[ok][i])


def _armed(flight: Flight, t: float) -> bool:
    return any(seg.start <= t < seg.end for seg in flight.segments)


def _family(flight: Flight) -> str:
    fw = flight.metadata.firmware or ""
    return "Plane" if "Plane" in fw else "Copter"


def telemetry(
    flight: Flight,
    path: Path,
    *,
    loss: float = 0.0,
    seed: int = 0,
    v2: bool = True,
    rates: dict[str, float] | None = None,
) -> Path:
    """Write ``flight`` as a ground station would have recorded it."""
    rng = random.Random(seed)
    rates = {**RATES, **(rates or {})}
    w = TlogWriter(v2=v2)
    t0 = (flight.metadata.start_time or DEFAULT_START).timestamp()
    end = float(flight.metadata.log_duration or 0.0)
    family = _family(flight)
    mode_numbers = {v: k for k, v in MODES_BY_FAMILY.get(family, {}).items()}
    mav_type = MAV_TYPE.get(flight.metadata.vehicle_type or "", 2)
    params = flight.parameters
    lo, hi = _pwm_range(params)
    motor_channel = {
        MOTOR_FUNCTIONS[int(v)]: int(k[5:-9])
        for k, v in params.items()
        if k.startswith("SERVO") and k.endswith("_FUNCTION") and int(v) in MOTOR_FUNCTIONS
    }
    records: list[tuple[float, tuple[int, int], str, dict[str, Any]]] = []

    def at(name: str, t: float, default: float = 0.0) -> float:
        return _at(flight, name, t, default)

    modes = [(e.t, e.text) for e in flight.events if e.kind is EventKind.MODE]

    def mode_at(t: float) -> int:
        current = modes[0][1] if modes else ""
        for mt, name in modes:
            if mt <= t:
                current = name
        return int(mode_numbers.get(current, 0))

    def every(name: str) -> Iterable[float]:
        rate = rates.get(name, 0.0)
        if rate <= 0:
            return []
        phase = rng.uniform(0, 1 / rate)  # streams are not in lockstep
        return np.arange(phase, end, 1 / rate).tolist()

    for t in np.arange(0.0, end, 1.0).tolist():
        records.append(
            (t, VEHICLE, "HEARTBEAT", {
                "type": mav_type, "autopilot": 3, "custom_mode": mode_at(t),
                "base_mode": 1 | (128 if _armed(flight, t) else 0),
                "system_status": 4 if _armed(flight, t) else 3, "mavlink_version": 3,
            })
        )  # fmt: skip
        records.append(
            (t + 0.3, GCS, "HEARTBEAT", {"type": 6, "autopilot": 8, "mavlink_version": 3})
        )
    names = sorted(params)
    for i, name in enumerate(names):  # the ground station downloads the parameters
        records.append(
            (0.5 + i * 0.002, VEHICLE, "PARAM_VALUE", {
                "param_id": name[:16], "param_value": params[name], "param_type": 9,
                "param_count": len(names), "param_index": i,
            })
        )  # fmt: skip
    for e in flight.events:
        if e.kind in (EventKind.MESSAGE, EventKind.ERROR, EventKind.FAILSAFE):
            sev = {EventKind.FAILSAFE: 2, EventKind.ERROR: 3}.get(e.kind, 6)
            if e.level == "critical":
                sev = 2
            records.append((e.t, VEHICLE, "STATUSTEXT", {"severity": sev, "text": e.text[:50]}))

    for t in every("ATTITUDE"):
        records.append((t, VEHICLE, "ATTITUDE", {
            "time_boot_ms": t * 1000,
            **{a: np.radians(at(f"att.{a}", t)) for a in ("roll", "pitch", "yaw")},
            **{f"{a}speed": np.radians(at(f"rate.{a}", t)) for a in ("roll", "pitch", "yaw")},
        }))  # fmt: skip
    for t in every("NAV_CONTROLLER_OUTPUT"):
        records.append((t, VEHICLE, "NAV_CONTROLLER_OUTPUT", {
            "nav_roll": at("att.des_roll", t), "nav_pitch": at("att.des_pitch", t),
        }))  # fmt: skip
    for t in every("GLOBAL_POSITION_INT"):
        records.append((t, VEHICLE, "GLOBAL_POSITION_INT", {
            "time_boot_ms": t * 1000, "lat": at("pos.lat", t) * 1e7, "lon": at("pos.lon", t) * 1e7,
            "alt": at("pos.alt", t) * 1000, "relative_alt": at("pos.rel_alt", t) * 1000,
            "vx": at("pos.vx", t) * 100, "vy": at("pos.vy", t) * 100,
            "vz": -at("pos.climb", t) * 100,
        }))  # fmt: skip
    for t in every("GPS_RAW_INT"):
        records.append((t, VEHICLE, "GPS_RAW_INT", {
            "time_usec": (t0 + t) * 1e6, "fix_type": at("gps.fix", t, 3),
            "lat": at("gps.lat", t) * 1e7, "lon": at("gps.lon", t) * 1e7,
            "alt": at("gps.alt", t) * 1000, "eph": at("gps.hdop", t, 0.8) * 100,
            "epv": 65535, "vel": at("gps.speed", t) * 100, "cog": 65535,
            "satellites_visible": at("gps.nsats", t, 12),
        }))  # fmt: skip
    for t in every("SYS_STATUS"):
        records.append((t, VEHICLE, "SYS_STATUS", {
            "voltage_battery": at("bat.volt", t) * 1000, "current_battery": at("bat.curr", t) * 100,
            "battery_remaining": at("bat.remaining", t, -0.01) * 100,
        }))  # fmt: skip
    for t in every("BATTERY_STATUS"):
        records.append((t, VEHICLE, "BATTERY_STATUS", {
            "current_consumed": at("bat.used_mah", t), "voltages": [65535] * 10,
            "battery_remaining": at("bat.remaining", t, -0.01) * 100,
        }))  # fmt: skip
    for t in every("VIBRATION"):
        records.append((t, VEHICLE, "VIBRATION", {
            "time_usec": t * 1e6, "vibration_x": at("vibe.x", t), "vibration_y": at("vibe.y", t),
            "vibration_z": at("vibe.z", t), "clipping_0": at("vibe.clip", t),
        }))  # fmt: skip
    for t in every("RAW_IMU"):
        records.append((t, VEHICLE, "RAW_IMU", {
            "time_usec": t * 1e6, **{f"{a}mag": at(f"mag.{a}", t) * 1000 for a in "xyz"},
        }))  # fmt: skip
    for t in every("SERVO_OUTPUT_RAW"):
        pwm = {
            f"servo{ch}_raw": lo + at(f"mot.{i}", t) * (hi - lo)
            for i, (_, ch) in enumerate(sorted(motor_channel.items()), start=1)
            if ch <= 16
        }
        records.append((t, VEHICLE, "SERVO_OUTPUT_RAW", {"time_usec": t * 1e6, **pwm}))
    for t in every("VFR_HUD"):
        records.append((t, VEHICLE, "VFR_HUD", {
            "throttle": at("ctrl.thrust", t) * 100, "alt": at("pos.alt", t),
            "climb": at("pos.climb", t), "groundspeed": at("gps.speed", t),
        }))  # fmt: skip
    for t in every("POWER_STATUS"):
        records.append((t, VEHICLE, "POWER_STATUS", {"Vcc": at("power.5v", t, 5.0) * 1000}))
    for t in every("EKF_STATUS_REPORT"):
        records.append((t, VEHICLE, "EKF_STATUS_REPORT", {
            "flags": 0x3FF, "velocity_variance": at("ekf.vel", t),
            "pos_horiz_variance": at("ekf.pos", t), "pos_vert_variance": at("ekf.hgt", t),
            "compass_variance": at("ekf.mag", t),
        }))  # fmt: skip
    for t in every("EXTENDED_SYS_STATE"):
        landed = at("state.landed", t, 0.0 if _armed(flight, t) else 1.0)
        records.append((t, VEHICLE, "EXTENDED_SYS_STATE", {"landed_state": 1 if landed else 2}))
    for t in every("SYSTEM_TIME"):
        records.append((t, VEHICLE, "SYSTEM_TIME", {
            "time_unix_usec": (t0 + t) * 1e6, "time_boot_ms": t * 1000,
        }))  # fmt: skip

    records.sort(key=lambda r: r[0])
    for t, sender, name, fields in records:
        rec_t = int((t0 + t) * 1e6)
        if loss and sender == VEHICLE and name != "PARAM_VALUE" and rng.random() < loss:
            w.frame(sender, name, fields)  # sent but lost: the sequence number moves on
            continue
        w.write(rec_t, sender, name, **fields)
    return w.save(path)
