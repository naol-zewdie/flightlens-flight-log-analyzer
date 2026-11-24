"""Demo data: generated flights that show what FlightLens finds.

``seed(data_dir)`` writes two small fleets of generated ArduPilot logs into a
FlightLens data folder and analyzes them in this process, so ``flightlens
demo`` opens on a populated library and aircraft page:

- a quadcopter: four healthy flights (its baseline), then one known fault per
  flight, a day apart;
- a fixed-wing: a healthy flight, then out of trim, a hard landing and a crash;
- a scripted manoeuvre flight (a 30° roll, a 45° pitch, a 90° turn and a 20 m
  square) that shows the Replay tab at its best.

Seeding twice is harmless: identical logs are recognised as duplicates.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from flightlens import parsers
from flightlens.sim.benchmark import Fault, _as_tlog, copter
from flightlens.sim.manoeuvre import manoeuvre_log
from flightlens.sim.plane import plane_log
from flightlens.store import Store
from flightlens.worker import process_log

Maker = Callable[[Path], Path]


def _quad(day: int, fault: Fault | None = None, seed: int = 7) -> Maker:
    return lambda p: copter(p, fault, seed, day=day)


DEMO: dict[str, Maker] = {
    "quad-01-healthy.bin": _quad(0, seed=0),
    "quad-02-healthy.bin": _quad(1, seed=1),
    "quad-03-healthy.bin": _quad(2, seed=2),
    "quad-04-healthy.bin": _quad(3, seed=3),
    "quad-05-gps-loss.bin": _quad(4, Fault("gps_loss", 70, 85)),
    "quad-06-battery-sag.bin": _quad(5, Fault("battery_sag", 100, 115, 3.15)),
    "quad-07-gyro-noise.bin": _quad(6, Fault("gyro_noise", level=12.0)),
    "quad-08-motor-failure.bin": _quad(7, Fault("motor_failure", 60, 80)),
    "quad-09-arming-refused.bin": _quad(8, Fault("arming_refused")),
    "quad-10-vibration.bin": _quad(9, Fault("vibration", 60, 140, 75)),
    # the next flight as the ground station recorded it over the radio
    "quad-11-telemetry.tlog": lambda p: _as_tlog(
        p, copter(p.with_suffix(".bin"), Fault("vibration", 60, 90, 42), 7, day=10)
    ),
    "plane-01-healthy.bin": lambda p: plane_log(p, "smooth", day=0),
    "plane-02-out-of-trim.bin": lambda p: plane_log(p, "smooth", trim=-0.2, day=2),
    "plane-03-hard-landing.bin": lambda p: plane_log(p, "hard", day=4),
    "plane-04-crash.bin": lambda p: plane_log(p, "crash", trim=-0.2, day=6),
    "quad-12-manoeuvres.bin": manoeuvre_log,
}


def seed(
    data_dir: Path,
    progress: Callable[[str], None] | None = None,
    only: Iterable[str] | None = None,
) -> list[str]:
    """Add the demo flights (or the ``only`` file names) to ``data_dir``; returns their ids."""
    store = Store(data_dir)
    ids: list[str] = []
    keep = set(only) if only is not None else set(DEMO)
    with tempfile.TemporaryDirectory() as tmp:
        for filename, make in DEMO.items():
            if filename not in keep:
                continue
            path = make(Path(tmp) / filename)
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            fmt = parsers.detect_format(path).value
            created = store.create_flight(path, sha, filename, fmt)
            if created is None:
                existing = store.find_by_sha(sha)
                if existing:
                    ids.append(existing["id"])
                continue
            flight_id, job_id = created
            if progress:
                progress(filename)
            original = store.original_path(flight_id)
            assert original is not None
            store.set_job(job_id, "processing")
            try:
                summary = process_log(str(original), str(store.flight_dir(flight_id)))
            except Exception as exc:  # a demo log that fails is shown as failed, not fatal
                store.set_job(job_id, "failed", f"Analysis error: {exc}")
            else:
                store.record_result(flight_id, summary)
                store.set_job(job_id, "done")
            ids.append(flight_id)
    return ids
