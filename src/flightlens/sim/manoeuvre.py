"""A scripted flight with exact ground truth, for checking replay and other
geometry against known attitudes and positions.

The quad takes off at 8 s, climbs to 15 m by 15.5 s, then flies:

- a 30° roll held 5 s (20-25 s), a 45° pitch held 5 s (30-35 s);
- a 90° yaw turn over 5 s (40-45 s);
- a 20 m square at 2 m/s (50-90 s): east, north, west, south;
- lands 95-102.5 s, disarms at 104 s; the log ends at 110 s.

``truth(t)`` gives the exact attitude and position at any time;
``manoeuvre_log(path)`` writes the flight as an ArduPilot ``.bin`` (attitude
as Euler angles at 50 Hz, position at 10 Hz). Each change happens along one
axis at a time and starts on a whole second, so linear interpolation between
logged samples reproduces the script exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from flightlens.sim.dfwriter import DFWriter

LAT0, LON0, ALT0 = -35.3632, 149.1652, 584.0
EARTH_RADIUS_M = 6_371_000.0
ARM, TAKEOFF, LAND, DISARM, END = 5.0, 8.0, 95.0, 104.0, 110.0
CLIMB_RATE, CRUISE_ALT, SIDE, SPEED = 2.0, 15.0, 20.0, 2.0
ATT_HZ, POS_HZ = 50, 10
BOOT_US = 30_000_000


@dataclass(frozen=True)
class Truth:
    roll: float
    pitch: float
    yaw: float
    east: float
    north: float
    up: float


def _ramp(t: float, start: float, hold_from: float, hold_to: float, end: float) -> float:
    """0 before start, rising linearly to 1 at hold_from, 1 until hold_to, back to 0 at end."""
    if t <= start or t >= end:
        return 0.0
    if t < hold_from:
        return (t - start) / (hold_from - start)
    if t <= hold_to:
        return 1.0
    return (end - t) / (end - hold_to)


def truth(t: float) -> Truth:
    roll = 30.0 * _ramp(t, 19, 20, 25, 26)
    pitch = 45.0 * _ramp(t, 29, 30, 35, 36)
    yaw = float(np.clip((t - 40) / 5, 0, 1) * 90.0)  # heading 0 → 90°, stays there
    # the square, starting and ending at the take-off point
    leg = SIDE / SPEED  # 10 s per side
    s = float(np.clip(t - 50, 0, 4 * leg))
    east = north = 0.0
    if s <= leg:
        east = SPEED * s
    elif s <= 2 * leg:
        east, north = SIDE, SPEED * (s - leg)
    elif s <= 3 * leg:
        east, north = SIDE - SPEED * (s - 2 * leg), SIDE
    else:
        east, north = 0.0, SIDE - SPEED * (s - 3 * leg)
    if t < TAKEOFF:
        up = 0.0
    elif t < LAND:
        up = min(CRUISE_ALT, (t - TAKEOFF) * CLIMB_RATE)
    else:
        up = max(0.0, CRUISE_ALT - (t - LAND) * CLIMB_RATE)
    return Truth(roll, pitch, yaw, east, north, up)


def to_latlon(east: float, north: float) -> tuple[float, float]:
    k = np.pi / 180
    lat = LAT0 + north / (EARTH_RADIUS_M * k)
    lon = LON0 + east / (EARTH_RADIUS_M * k * np.cos(LAT0 * k))
    return float(lat), float(lon)


def manoeuvre_log(path: Path) -> Path:
    w = DFWriter()
    w.define("PARM", "QNf", "TimeUS,Name,Value")
    w.define("MSG", "QZ", "TimeUS,Message")
    w.define("EV", "QB", "TimeUS,Id")
    w.define("ARM", "QBIBB", "TimeUS,ArmState,ArmChecks,Forced,Method")
    w.define("MODE", "QMBB", "TimeUS,Mode,ModeNum,Rsn")
    w.define("ATT", "QccccCCCC", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,ErrRP,ErrYaw")
    w.define("POS", "QLLff", "TimeUS,Lat,Lng,Alt,RelHomeAlt")
    w.define("GPS", "QBBIHBcLLeff", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,VZ")
    w.define("XKF1", "QBfff", "TimeUS,C,VN,VE,VD")

    def us(t: float) -> int:
        return BOOT_US + round(t * 1e6)

    w.write("MSG", TimeUS=us(0), Message="ArduCopter V4.6.2 (1ebd4d99)")
    w.write("MSG", TimeUS=us(0), Message="Frame: QUAD/X")
    w.write("MODE", TimeUS=us(0), Mode=0, ModeNum=0, Rsn=0)
    for k in range(int(END * ATT_HZ) + 1):
        t = k / ATT_HZ
        if t == ARM:
            w.write("EV", TimeUS=us(t), Id=10)
            w.write("ARM", TimeUS=us(t), ArmState=1)
            w.write("MODE", TimeUS=us(t), Mode=5, ModeNum=5, Rsn=1)  # Loiter
        if t == DISARM:
            w.write("EV", TimeUS=us(t), Id=11)
            w.write("ARM", TimeUS=us(t), ArmState=0)
        g = truth(t)
        # ATT stores centi-degrees (c, C types); yaw is 0-360
        w.write("ATT", TimeUS=us(t), DesRoll=g.roll, Roll=g.roll, DesPitch=g.pitch,
                Pitch=g.pitch, DesYaw=g.yaw % 360, Yaw=g.yaw % 360)  # fmt: skip
        if k % (ATT_HZ // POS_HZ) == 0:
            lat, lon = to_latlon(g.east, g.north)
            w.write("POS", TimeUS=us(t), Lat=lat, Lng=lon, Alt=ALT0 + g.up, RelHomeAlt=g.up)
            w.write("GPS", TimeUS=us(t), I=0, Status=3, GMS=0, GWk=0, NSats=16, HDop=0.7,
                    Lat=lat, Lng=lon, Alt=ALT0 + g.up, Spd=0.0, VZ=0.0)  # fmt: skip
            d = 1 / POS_HZ
            a, b = truth(max(0.0, t - d)), truth(t + d)
            w.write("XKF1", TimeUS=us(t), C=0, VN=(b.north - a.north) / (2 * d),
                    VE=(b.east - a.east) / (2 * d), VD=-(b.up - a.up) / (2 * d))  # fmt: skip
    return w.save(path)
