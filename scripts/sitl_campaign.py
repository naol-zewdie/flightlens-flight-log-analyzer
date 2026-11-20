"""Fly ArduCopter SITL once per fault and label the logs for ``flightlens validate``.

Runs on Linux or WSL with a built ArduPilot tree (``./waf configure --board sitl
&& ./waf copter``) and ``pip install pymavlink``. Each flight: start a fresh SITL,
arm in Guided, take off to 15 m, hover 40 s, inject the fault, hover 40 s,
land. The logs and ``campaign.yaml`` go to ``--out``::

    python scripts/sitl_campaign.py --ardupilot ~/ardupilot --out sitl-logs
    flightlens validate sitl-logs

SIM_* parameter names change between ArduPilot versions; a fault whose
parameters this build does not have is skipped and reported. Labels are
deliberately loose (the check must fire, at any severity): how strongly a
simulated fault shows up depends on the build, so the severity is not part of
what this campaign proves. Logs are never committed; keep them local or attach
them to a release.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Fault:
    name: str
    fault: str
    # alternatives: the first parameter set whose names all exist is used
    params: list[dict[str, float]] = field(default_factory=list)
    expect: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)


FAULTS = [
    Fault("healthy-1", "healthy"),
    Fault("healthy-2", "healthy"),
    Fault(
        "vibration",
        "vibration",
        [
            {"SIM_VIB_MOT_MAX": 250, "SIM_VIB_MOT_MULT": 4},
            {"SIM_VIB_FREQ_Z": 120, "SIM_ACC1_RND": 6},
        ],
        ["vibration.level"],
        ["imu.clipping", "estimator.health", "attitude.tracking"],
    ),
    Fault(
        "accel-noise",
        "vibration",
        [{"SIM_ACC1_RND": 8, "SIM_ACC2_RND": 8, "SIM_ACC3_RND": 8}],
        ["vibration.level"],
        ["imu.clipping", "estimator.health"],
    ),
    Fault(
        "gps-loss",
        "GPS",
        [{"SIM_GPS1_ENABLE": 0}, {"SIM_GPS_DISABLE": 1}],
        ["gps.quality"],
        ["estimator.health", "events.failsafes"],
    ),
    Fault(
        "gps-few-sats",
        "GPS",
        [{"SIM_GPS1_NUMSATS": 5}, {"SIM_GPS_NUMSATS": 5}],
        ["gps.quality"],
        ["estimator.health"],
    ),
    Fault(
        "motor-weak",
        "motor",
        [{"SIM_ENGINE_MUL": 0.6, "SIM_ENGINE_FAIL": 1}],
        ["motor.imbalance"],
        ["motor.saturation", "attitude.tracking"],
    ),
    Fault(
        "battery-low",
        "battery",
        [{"SIM_BATT_VOLTAGE": 10.1}],
        ["battery.sag"],
        ["events.failsafes"],
    ),
    Fault(
        "radio-failsafe",
        "failsafe",
        [{"FS_THR_ENABLE": 1, "SIM_RC_FAIL": 1}],
        ["events.failsafes"],
        ["flight.crash"],
    ),
]


def run_flight(ap: Path, fault: Fault, workdir: Path, speedup: int) -> Path | None:
    from pymavlink import mavutil  # imported here so --help works without it

    binary = ap / "build" / "sitl" / "bin" / "arducopter"
    defaults = ap / "Tools" / "autotest" / "default_params" / "copter.parm"
    workdir.mkdir(parents=True)
    proc = subprocess.Popen(
        [str(binary), "--model", "quad", "--speedup", str(speedup), "--defaults", str(defaults),
         "-I0", "--home", "-35.363261,149.165230,584,353"],
        cwd=workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        m = mavutil.mavlink_connection("tcp:127.0.0.1:5760", retries=60)
        m.wait_heartbeat(timeout=60)

        def param(name: str, value: float) -> None:
            m.mav.param_set_send(m.target_system, m.target_component, name.encode(), value, 9)
            time.sleep(0.2)

        def has(name: str) -> bool:
            m.mav.param_request_read_send(m.target_system, m.target_component, name.encode(), -1)
            msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=3)
            return msg is not None and msg.param_id == name

        def sim_wait(seconds: float) -> None:
            end = time.time() + seconds / speedup
            while time.time() < end:
                m.recv_match(blocking=True, timeout=0.5)

        chosen = next((p for p in fault.params if all(has(k) for k in p)), None)
        if fault.params and chosen is None:
            print(f"  skip {fault.name}: this build has none of {fault.params}")
            return None
        param("LOG_DISARMED", 0)
        sim_wait(30)  # EKF and GPS settle
        m.set_mode("GUIDED")
        m.arducopter_arm()
        m.motors_armed_wait()
        takeoff = mavutil.mavlink.MAV_CMD_NAV_TAKEOFF
        m.mav.command_long_send(
            m.target_system, m.target_component, takeoff, 0, 0, 0, 0, 0, 0, 0, 15
        )
        sim_wait(60)
        for k, v in (chosen or {}).items():
            param(k, v)
        sim_wait(40)
        m.set_mode("LAND")
        m.motors_disarmed_wait()
        sim_wait(5)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    logs = sorted((workdir / "logs").glob("*.BIN"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ardupilot", type=Path, required=True, help="built ArduPilot tree")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--speedup", type=int, default=5)
    ap.add_argument("--only", nargs="*", help="fault names to run")
    args = ap.parse_args()
    if not (args.ardupilot / "build" / "sitl" / "bin" / "arducopter").exists():
        sys.exit(
            "arducopter SITL binary not found: run ./waf configure --board sitl && ./waf copter"
        )
    args.out.mkdir(parents=True, exist_ok=True)

    labels: dict[str, Any] = {}
    for fault in FAULTS:
        if args.only and fault.name not in args.only:
            continue
        print(f"flying {fault.name} …")
        with tempfile.TemporaryDirectory() as tmp:
            log = run_flight(args.ardupilot, fault, Path(tmp) / "sitl", args.speedup)
            if log is None:
                continue
            name = f"sitl-copter-{fault.name}.bin"
            shutil.copy(log, args.out / name)
        entry: dict[str, Any] = {"fault": fault.fault, "expect": fault.expect, "exhaustive": True}
        if fault.fault == "healthy":
            entry["verdict"] = "healthy"
        if fault.allow:
            entry["allow"] = fault.allow
        labels[name] = entry

    manifest = {
        "source": "ArduCopter SITL, scripts/sitl_campaign.py",
        "logs_dir": ".",
        "logs": labels,
    }
    path = args.out / "campaign.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    print(f"{len(labels)} logs; now run: flightlens validate {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
