"""Low-level reader for MAVLink telemetry logs (``.tlog``).

A tlog is what a ground station (Mission Planner, QGroundControl, MAVProxy)
received over the telemetry link: every record is an 8-byte big-endian
timestamp (microseconds since 1970, the ground station's clock) followed by
one MAVLink frame, version 1 or 2::

    v1: FE len seq sys comp msgid      payload crc[2]
    v2: FD len incompat compat seq sys comp msgid[3] payload crc[2] [signature 13]

Only the messages FlightLens uses are defined here (``DEFINITIONS``); their
wire layout and checksum seed (``crc_extra``) are derived from the field list
exactly as the MAVLink generator does, and ``tests/test_tlog.py`` checks them
against the reference implementation. Every frame of a known message is
checked with its CRC; damaged bytes are skipped by searching for the next
record, and counted. Unknown messages are skipped by their length.

This module only turns bytes into columns; what they mean is the job of
:mod:`flightlens.parsers.tlog`.
"""

from __future__ import annotations

import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

STX_V1, STX_V2 = 0xFE, 0xFD
TIMESTAMP_BYTES = 8
SIGNATURE_BYTES = 13
MIN_UNIX_US = 946_684_800_000_000  # 2000-01-01
MAX_UNIX_US = 4_102_444_800_000_000  # 2100-01-01

# MAVLink type -> (struct code, size). char arrays are text.
TYPES: dict[str, tuple[str, int]] = {
    "uint8_t": ("B", 1),
    "int8_t": ("b", 1),
    "char": ("s", 1),
    "uint16_t": ("H", 2),
    "int16_t": ("h", 2),
    "uint32_t": ("I", 4),
    "int32_t": ("i", 4),
    "float": ("f", 4),
    "uint64_t": ("Q", 8),
    "int64_t": ("q", 8),
    "double": ("d", 8),
}

# id: (name, fields in declaration order; fields after "|" are extensions)
DEFINITIONS: dict[int, tuple[str, str]] = {
    0: (
        "HEARTBEAT",
        "uint8_t type; uint8_t autopilot; uint8_t base_mode; uint32_t custom_mode; "
        "uint8_t system_status; uint8_t_mavlink_version mavlink_version",
    ),
    1: (
        "SYS_STATUS",
        "uint32_t onboard_control_sensors_present; uint32_t onboard_control_sensors_enabled; "
        "uint32_t onboard_control_sensors_health; uint16_t load; uint16_t voltage_battery; "
        "int16_t current_battery; int8_t battery_remaining; uint16_t drop_rate_comm; "
        "uint16_t errors_comm; uint16_t errors_count1; uint16_t errors_count2; "
        "uint16_t errors_count3; uint16_t errors_count4 | "
        "uint32_t onboard_control_sensors_present_extended; "
        "uint32_t onboard_control_sensors_enabled_extended; "
        "uint32_t onboard_control_sensors_health_extended",
    ),
    2: ("SYSTEM_TIME", "uint64_t time_unix_usec; uint32_t time_boot_ms"),
    22: (
        "PARAM_VALUE",
        "char param_id[16]; float param_value; uint8_t param_type; uint16_t param_count; "
        "uint16_t param_index",
    ),
    24: (
        "GPS_RAW_INT",
        "uint64_t time_usec; uint8_t fix_type; int32_t lat; int32_t lon; int32_t alt; "
        "uint16_t eph; uint16_t epv; uint16_t vel; uint16_t cog; uint8_t satellites_visible | "
        "int32_t alt_ellipsoid; uint32_t h_acc; uint32_t v_acc; uint32_t vel_acc; "
        "uint32_t hdg_acc; uint16_t yaw",
    ),
    27: (
        "RAW_IMU",
        "uint64_t time_usec; int16_t xacc; int16_t yacc; int16_t zacc; int16_t xgyro; "
        "int16_t ygyro; int16_t zgyro; int16_t xmag; int16_t ymag; int16_t zmag | "
        "uint8_t id; int16_t temperature",
    ),
    30: (
        "ATTITUDE",
        "uint32_t time_boot_ms; float roll; float pitch; float yaw; float rollspeed; "
        "float pitchspeed; float yawspeed",
    ),
    33: (
        "GLOBAL_POSITION_INT",
        "uint32_t time_boot_ms; int32_t lat; int32_t lon; int32_t alt; int32_t relative_alt; "
        "int16_t vx; int16_t vy; int16_t vz; uint16_t hdg",
    ),
    36: (
        "SERVO_OUTPUT_RAW",
        "uint32_t time_usec; uint8_t port; "
        + "; ".join(f"uint16_t servo{i}_raw" for i in range(1, 9))
        + " | "
        + "; ".join(f"uint16_t servo{i}_raw" for i in range(9, 17)),
    ),
    62: (
        "NAV_CONTROLLER_OUTPUT",
        "float nav_roll; float nav_pitch; int16_t nav_bearing; int16_t target_bearing; "
        "uint16_t wp_dist; float alt_error; float aspd_error; float xtrack_error",
    ),
    65: (
        "RC_CHANNELS",
        "uint32_t time_boot_ms; uint8_t chancount; "
        + "; ".join(f"uint16_t chan{i}_raw" for i in range(1, 19))
        + "; uint8_t rssi",
    ),
    74: (
        "VFR_HUD",
        "float airspeed; float groundspeed; int16_t heading; uint16_t throttle; float alt; "
        "float climb",
    ),
    83: (
        "ATTITUDE_TARGET",
        "uint32_t time_boot_ms; uint8_t type_mask; float q[4]; float body_roll_rate; "
        "float body_pitch_rate; float body_yaw_rate; float thrust",
    ),
    125: ("POWER_STATUS", "uint16_t Vcc; uint16_t Vservo; uint16_t flags"),
    147: (
        "BATTERY_STATUS",
        "uint8_t id; uint8_t battery_function; uint8_t type; int16_t temperature; "
        "uint16_t voltages[10]; int16_t current_battery; int32_t current_consumed; "
        "int32_t energy_consumed; int8_t battery_remaining | int32_t time_remaining; "
        "uint8_t charge_state; uint16_t voltages_ext[4]; uint8_t mode; uint32_t fault_bitmask",
    ),
    148: (
        "AUTOPILOT_VERSION",
        "uint64_t capabilities; uint32_t flight_sw_version; uint32_t middleware_sw_version; "
        "uint32_t os_sw_version; uint32_t board_version; uint8_t flight_custom_version[8]; "
        "uint8_t middleware_custom_version[8]; uint8_t os_custom_version[8]; uint16_t vendor_id; "
        "uint16_t product_id; uint64_t uid | uint8_t uid2[18]",
    ),
    193: (
        "EKF_STATUS_REPORT",
        "uint16_t flags; float velocity_variance; float pos_horiz_variance; "
        "float pos_vert_variance; float compass_variance; float terrain_alt_variance | "
        "float airspeed_variance",
    ),
    230: (
        "ESTIMATOR_STATUS",
        "uint64_t time_usec; uint16_t flags; float vel_ratio; float pos_horiz_ratio; "
        "float pos_vert_ratio; float mag_ratio; float hagl_ratio; float tas_ratio; "
        "float pos_horiz_accuracy; float pos_vert_accuracy",
    ),
    241: (
        "VIBRATION",
        "uint64_t time_usec; float vibration_x; float vibration_y; float vibration_z; "
        "uint32_t clipping_0; uint32_t clipping_1; uint32_t clipping_2",
    ),
    245: ("EXTENDED_SYS_STATE", "uint8_t vtol_state; uint8_t landed_state"),
    253: ("STATUSTEXT", "uint8_t severity; char text[50] | uint16_t id; uint8_t chunk_seq"),
}


@dataclass(frozen=True)
class FieldDef:
    ctype: str
    name: str
    length: int
    """Array length; 0 for a scalar."""

    @property
    def base(self) -> str:
        return "uint8_t" if self.ctype == "uint8_t_mavlink_version" else self.ctype

    @property
    def size(self) -> int:
        return TYPES[self.base][1] * max(self.length, 1)


@dataclass(frozen=True)
class MessageDef:
    id: int
    name: str
    fields: tuple[FieldDef, ...]
    """In wire order: base fields by type size (largest first), then extensions."""
    n_base: int
    crc_extra: int
    struct: struct.Struct

    @property
    def length(self) -> int:
        return self.struct.size

    def columns(self) -> list[str]:
        """Column names after decoding; arrays become ``name[0]``..; char arrays one text."""
        out = []
        for f in self.fields:
            if f.length and f.base != "char":
                out += [f"{f.name}[{i}]" for i in range(f.length)]
            else:
                out.append(f.name)
        return out


def x25(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX, the MAVLink checksum."""
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _parse_fields(spec: str) -> tuple[list[FieldDef], list[FieldDef]]:
    base_s, _, ext_s = spec.partition("|")

    def parse(part: str) -> list[FieldDef]:
        out = []
        for item in (x.strip() for x in part.split(";")):
            if not item:
                continue
            ctype, name = item.split()
            length = 0
            if "[" in name:
                name, n = name[:-1].split("[")
                length = int(n)
            out.append(FieldDef(ctype, name, length))
        return out

    return parse(base_s), parse(ext_s)


def _build(msg_id: int, name: str, spec: str) -> MessageDef:
    base, ext = _parse_fields(spec)
    # wire order: base fields sorted by element size, largest first (stable)
    wire = sorted(base, key=lambda f: -TYPES[f.base][1]) + ext
    crc = x25(f"{name} ".encode())
    for f in sorted(base, key=lambda f: -TYPES[f.base][1]):
        crc = x25(f"{f.base} {f.name} ".encode(), crc)
        if f.length:
            crc = x25(bytes([f.length]), crc)
    crc_extra = (crc & 0xFF) ^ (crc >> 8)
    fmt = "<" + "".join(
        f"{f.length}s"
        if f.base == "char"
        else (f"{f.length}{TYPES[f.base][0]}" if f.length else TYPES[f.base][0])
        for f in wire
    )
    return MessageDef(msg_id, name, tuple(wire), len(base), crc_extra, struct.Struct(fmt))


MESSAGES: dict[int, MessageDef] = {i: _build(i, n, s) for i, (n, s) in DEFINITIONS.items()}
BY_NAME: dict[str, MessageDef] = {m.name: m for m in MESSAGES.values()}


class TlogParseError(Exception):
    pass


Columns = dict[str, NDArray[Any]]


@dataclass
class Stream:
    """Messages of one type from one sender, as columns plus receive time (s, unix)."""

    rows: list[tuple[Any, ...]] = field(default_factory=list)
    t: list[float] = field(default_factory=list)


@dataclass
class TlogData:
    streams: dict[tuple[int, int, str], Stream]
    """(system id, component id, message name) -> rows."""
    heartbeats: dict[tuple[int, int], int]
    packets: int = 0
    unknown: int = 0
    crc_errors: int = 0
    bytes_skipped: int = 0
    lost: dict[tuple[int, int], int] = field(default_factory=dict)
    """Sequence numbers missing per sender: packets the radio lost."""
    received: dict[tuple[int, int], int] = field(default_factory=dict)

    def columns(self, sysid: int, compid: int, name: str) -> tuple[NDArray[np.float64], Columns]:
        """Receive times (unix seconds) and decoded columns of one message type."""
        s = self.streams.get((sysid, compid, name))
        msg = BY_NAME[name]
        if s is None or not s.rows:
            return np.zeros(0), {}
        names = msg.columns()
        cols: Columns = {}
        transposed = list(zip(*s.rows, strict=True))
        for n, values in zip(names, transposed, strict=True):
            if isinstance(values[0], bytes):
                cols[n] = np.array(values, dtype=object)
            else:
                cols[n] = np.asarray(values, dtype=np.float64)
        return np.asarray(s.t, dtype=np.float64), cols


def read(path: Path) -> TlogData:
    data = path.read_bytes()
    n = len(data)
    out = TlogData(streams=defaultdict(Stream), heartbeats=defaultdict(int))
    last_seq: dict[tuple[int, int], int] = {}
    pos = 0
    last_ts: int | None = None

    def plausible(ts: int) -> bool:
        if not MIN_UNIX_US <= ts <= MAX_UNIX_US:
            return False
        return last_ts is None or abs(ts - last_ts) < 86_400_000_000

    while pos + TIMESTAMP_BYTES + 8 <= n:
        ts = int.from_bytes(data[pos : pos + TIMESTAMP_BYTES], "big")
        p = pos + TIMESTAMP_BYTES
        stx = data[p]
        frame = None
        if plausible(ts) and stx in (STX_V1, STX_V2):
            frame = _frame(data, p, stx)
        if frame is None:
            # not a record here: search for the next plausible one
            pos = _resync(data, pos + 1, plausible)
            if pos < 0:
                out.bytes_skipped += n - p + TIMESTAMP_BYTES
                break
            continue
        end, sysid, compid, seq, msg_id, payload, crc_ok = frame
        if msg_id in MESSAGES and not crc_ok:
            out.crc_errors += 1
            nxt = _resync(data, pos + 1, plausible)
            out.bytes_skipped += (nxt if nxt >= 0 else n) - pos
            if nxt < 0:
                break
            pos = nxt
            continue
        last_ts = ts
        out.packets += 1
        key = (sysid, compid)
        out.received[key] = out.received.get(key, 0) + 1
        if key in last_seq:
            gap = (seq - last_seq[key] - 1) % 256
            if gap < 128:  # a larger jump is a reboot or a second link, not loss
                out.lost[key] = out.lost.get(key, 0) + gap
        last_seq[key] = seq
        msg = MESSAGES.get(msg_id)
        if msg is None:
            out.unknown += 1
        else:
            body = payload[: msg.length].ljust(msg.length, b"\0")  # v2 drops trailing zeros
            values = msg.struct.unpack(body)
            out.streams[(sysid, compid, msg.name)].rows.append(values)
            out.streams[(sysid, compid, msg.name)].t.append(ts / 1e6)
            if msg_id == 0:
                out.heartbeats[key] += 1
        pos = end
    if out.packets == 0:
        raise TlogParseError("no MAVLink packets found")
    out.streams = dict(out.streams)
    out.heartbeats = dict(out.heartbeats)
    return out


def _frame(data: bytes, p: int, stx: int) -> tuple[int, int, int, int, int, bytes, bool] | None:
    """(end, sysid, compid, seq, msgid, payload, crc ok) of the frame at ``p``."""
    n = len(data)
    if stx == STX_V1:
        if p + 6 > n:
            return None
        length = data[p + 1]
        seq, sysid, compid, msg_id = data[p + 2], data[p + 3], data[p + 4], data[p + 5]
        head = 6
        end = p + head + length + 2
    else:
        if p + 10 > n:
            return None
        length = data[p + 1]
        incompat = data[p + 2]
        if incompat & ~0x01:
            return None  # unknown incompatibility flag: cannot be read
        seq, sysid, compid = data[p + 4], data[p + 5], data[p + 6]
        msg_id = data[p + 7] | data[p + 8] << 8 | data[p + 9] << 16
        head = 10
        end = p + head + length + 2 + (SIGNATURE_BYTES if incompat & 0x01 else 0)
    if end > n:
        return None
    payload = data[p + head : p + head + length]
    msg = MESSAGES.get(msg_id)
    crc_ok = True
    if msg is not None:
        if stx == STX_V1 and length != msg.length and length != _base_length(msg):
            return None
        crc = x25(data[p + 1 : p + head + length])
        crc = x25(bytes([msg.crc_extra]), crc)
        got = data[p + head + length] | data[p + head + length + 1] << 8
        crc_ok = crc == got
    return end, sysid, compid, seq, msg_id, payload, crc_ok


def _base_length(msg: MessageDef) -> int:
    return sum(f.size for f in msg.fields[: msg.n_base])


def _resync(data: bytes, start: int, plausible: Any) -> int:
    """Position of the next record that starts with a plausible timestamp and an STX."""
    n = len(data)
    i = start
    while i + TIMESTAMP_BYTES < n:
        j1 = data.find(bytes([STX_V1]), i + TIMESTAMP_BYTES)
        j2 = data.find(bytes([STX_V2]), i + TIMESTAMP_BYTES)
        cands = [j for j in (j1, j2) if j >= 0]
        if not cands:
            return -1
        j = min(cands)
        pos = j - TIMESTAMP_BYTES
        ts = int.from_bytes(data[pos:j], "big")
        if plausible(ts) and _frame(data, j, data[j]) is not None:
            return pos
        i = pos + 1
    return -1
