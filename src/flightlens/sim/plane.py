"""ArduPlane flight: runway takeoff at 20 s, Stabilize, and one of three endings.

Modelled on real ArduPlane crash logs: Stabilize mode has no yaw or roll
setpoint (DesRoll/DesYaw stay 0).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from flightlens.sim.dfwriter import DFWriter

BOOT = 30_000_000
HOME = (-35.0, 149.0)
# GPS time of the log start: 2026-03-08 09:00:00 UTC (+18 leap seconds)
GPS_WEEK, GPS_MS0 = 2409, 32_418_000


def _latlon(x: float, y: float) -> tuple[float, float]:
    """Metres east/north of home to degrees."""
    return HOME[0] + y / 111_320, HOME[1] + x / (111_320 * np.cos(np.radians(HOME[0])))


def plane_log(path: Path, ending: str = "smooth", trim: float = 0.0, day: int = 0) -> Path:
    """ending: "smooth" (landing at 100 s), "hard" (impact on touchdown), "crash".

    ``day`` dates the flight that many days after the first one.
    """
    w = DFWriter()
    w.define("PARM", "QNf", "TimeUS,Name,Value")
    w.define("MSG", "QZ", "TimeUS,Message")
    w.define("EV", "QB", "TimeUS,Id")
    w.define("ARM", "QBIBB", "TimeUS,ArmState,ArmChecks,Forced,Method")
    w.define("MODE", "QMBB", "TimeUS,Mode,ModeNum,Rsn")
    w.define("STAT", "QBfBBBBBB", "TimeUS,isFlying,isFlyProb,Armed,Safety,Crash,Still,Stage,Hit")
    w.define("ATT", "QccccCCCC", "TimeUS,DesRoll,Roll,DesPitch,Pitch,DesYaw,Yaw,ErrRP,ErrYaw")
    w.define("AETR", "Qhhhhhhf", "TimeUS,Ail,Elev,Thr,Rudd,Flap,Steer,SS")
    w.define("VIBE", "QBfffI", "TimeUS,IMU,VibeX,VibeY,VibeZ,Clip")
    w.define("BAT", "QBfffB", "TimeUS,Inst,Volt,Curr,CurrTot,RemPct")
    w.define("GPS", "QBBIHBcLLeff", "TimeUS,I,Status,GMS,GWk,NSats,HDop,Lat,Lng,Alt,Spd,VZ")
    w.define("POS", "QLLff", "TimeUS,Lat,Lng,Alt,RelHomeAlt")
    w.define("XKF4", "QBcccc", "TimeUS,C,SV,SP,SH,SM")

    def us(t: float) -> int:
        return BOOT + round(t * 1e6)

    for name, value in {"ROLL_LIMIT_DEG": 45, "PTCH_LIM_MAX_DEG": 20}.items():
        w.write("PARM", TimeUS=us(0), Name=name, Value=value)
    w.write("MSG", TimeUS=us(0), Message="ArduPlane V4.6.2 (1ebd4d99)")
    w.write("MSG", TimeUS=us(0), Message="MatekH743 0039002A 3438510F 37383539")  # made-up board
    w.write("MODE", TimeUS=us(0), Mode=2, ModeNum=2, Rsn=0)

    end = 92.0 if ending == "crash" else 110.0
    land = 100.0
    clip = 0
    x = y = 0.0  # metres east / north of the runway threshold
    heading = 90.0  # runway heading, east
    for k in range(int(end * 10)):
        t = k / 10
        if t == 5.0:
            w.write("ARM", TimeUS=us(t), ArmState=1)
            w.write("EV", TimeUS=us(t), Id=10)
        if t == 105.0 and ending != "crash":
            w.write("ARM", TimeUS=us(t), ArmState=0)
            w.write("EV", TimeUS=us(t), Id=11)
        armed = t >= 5 and (ending == "crash" or t < 105)
        flying = 20 <= t < (89.5 if ending == "crash" else land)

        roll, ail, impact = 1.0 * np.sin(t), trim, False
        if ending == "crash":
            if 60 <= t < 70:
                roll = -60.0  # bank beyond the 45° limit
            if 84 <= t < 89.5:
                roll, ail = -60.0 - (t - 84) * 15, -1.0  # rolling over, aileron at full travel
            if 89.5 <= t < 90.0:
                impact = True
            if t >= 89.5:
                roll, ail = -170.0, -1.0  # at rest, inverted
        elif ending == "hard" and land - 0.2 <= t < land + 0.2:
            impact = True

        alt = 0.0 if not flying else min(50.0, (t - 20) * 5)
        if ending == "crash" and t >= 89.5:
            alt = 0.0
        if ending != "crash" and t >= land:
            alt = 0.0
        thr = 80 if flying else (30 if armed else 0)
        w.write("STAT", TimeUS=us(t), isFlying=int(flying), Armed=int(armed))
        w.write("ATT", TimeUS=us(t), DesRoll=0, Roll=roll, DesPitch=0, Pitch=3, DesYaw=0,
                Yaw=(t * 3) % 360)  # fmt: skip
        w.write("AETR", TimeUS=us(t), Ail=round(ail * 4500), Elev=0, Thr=thr)
        v = 90.0 if impact else 6.0
        if impact:
            clip += 50
        w.write("VIBE", TimeUS=us(t), IMU=0, VibeX=v, VibeY=v, VibeZ=v, Clip=clip)
        volt = 23.5 if flying else 25.0
        w.write("BAT", TimeUS=us(t), Inst=0, Volt=volt, Curr=30 if flying else 1, CurrTot=t * 5)
        # ground track: take-off roll east from 15 s, then a left-hand circuit
        on_ground = t < 15 or (alt == 0.0 and not flying and t > 20)
        speed = 0.0 if on_ground else min(20.0, (t - 15) * 4) if t < 20 else 20.0
        if flying and t >= 25:
            heading = (heading - 6.0 * 0.1) % 360  # 6°/s turn: a circuit of about 400 m
        x += speed * 0.1 * np.sin(np.radians(heading))
        y += speed * 0.1 * np.cos(np.radians(heading))
        lat, lng = _latlon(x, y)
        week, ms0 = divmod(GPS_MS0 + day * 86_400_000, 604_800_000)
        w.write("GPS", TimeUS=us(t), I=0, Status=3, GMS=ms0 + round(t * 1000),
                GWk=GPS_WEEK + week, NSats=12, HDop=0.8, Lat=lat, Lng=lng,
                Alt=600 + alt, Spd=speed)  # fmt: skip
        w.write("POS", TimeUS=us(t), Lat=lat, Lng=lng, Alt=600 + alt, RelHomeAlt=alt)
        w.write("XKF4", TimeUS=us(t), C=0, SV=0.1, SP=0.1, SH=0.1, SM=0.1)
    return w.save(path)
