"""Per-flight health metrics, for trends across the flights of one aircraft.

A fixed catalogue: each metric has a unit and a direction (which way is
worse), plus how large a change against the aircraft's own baseline counts as
drift (both a relative and an absolute minimum, so tiny numbers do not alarm).
Metrics come from the signals (in the air, impacts excluded) or from what a
check already measured; a metric that cannot be computed is left out, never 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from flightlens.checks._util import impact_windows, in_air_mask, outside
from flightlens.checks.battery import PLAUSIBLE_CELL_V, cell_count, internal_resistance
from flightlens.model import Flight
from flightlens.summary import summarize

if TYPE_CHECKING:
    from flightlens.engine import Report


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    unit: str
    worse: int
    """+1 when higher is worse, -1 when lower is worse, 0 for context only."""
    rel: float
    """Drift: worse than the baseline by at least this fraction of it ..."""
    abs_min: float
    """... and by at least this much in absolute terms."""
    meaning: str
    cause: str = ""
    """Usual causes, for a drift alert."""


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec(
        "score",
        "Health score",
        "",
        -1,
        0.0,
        20,
        "the overall verdict of each flight",
        "See the findings of that flight.",
    ),
    MetricSpec(
        "vibration",
        "Vibration",
        "m/s²",
        +1,
        0.3,
        3.0,
        "95th percentile of the worst axis in the air: props, motors, mounts",
        "Check props for damage and balance, motor bearings and the autopilot mount.",
    ),
    MetricSpec(
        "clipping",
        "IMU clipping",
        "events/min",
        +1,
        0.5,
        5.0,
        "accelerometer saturation per minute in the air",
        "Usually vibration beyond the sensor range: check props and the autopilot mount.",
    ),
    MetricSpec(
        "battery_ir",
        "Battery internal resistance",
        "mΩ/cell",
        +1,
        0.25,
        2.0,
        "rises as a pack ages; compare packs by flying them in turn",
        "The pack is ageing or damaged; check which pack was used, and its connectors.",
    ),
    MetricSpec(
        "cell_min",
        "Lowest cell voltage",
        "V",
        -1,
        0.0,
        0.1,
        "1st percentile per cell in the air: pack health and how far it was flown",
        "A weaker or less charged pack, or a longer or harder flight.",
    ),
    MetricSpec(
        "motor_output",
        "Motor output",
        "%",
        +1,
        0.1,
        5.0,
        "median mean motor output in the air: more power for the same flight means "
        "worn props or motors, or added weight",
        "Check props and motors for wear, and whether payload or battery weight changed.",
    ),
    MetricSpec(
        "imbalance",
        "Motor imbalance",
        "%",
        +1,
        0.5,
        4.0,
        "spread between motors in steady hover: CG, bent arm, a failing motor",
        "Check the centre of gravity, arms and motor mounts, and the weakest motor.",
    ),
    MetricSpec(
        "ekf",
        "Estimator innovations",
        "ratio",
        +1,
        0.5,
        0.2,
        "99th percentile of the worst test ratio: sensor disagreement",
        "Sensors disagree more than before: check compass interference, GPS and IMU health.",
    ),
    MetricSpec(
        "gyro_noise",
        "Gyro noise",
        "deg/s",
        +1,
        0.5,
        0.5,
        "the noise peak left after the gyro low-pass",
        "Props, motors or mounts are noisier; check them before retuning filters.",
    ),
    MetricSpec(
        "trim",
        "Aileron trim",
        "%",
        +1,
        0.5,
        5.0,
        "aileron needed to fly level (fixed wing): a warped wing, linkage or CG",
        "A warped wing, a moved servo linkage or a CG change; re-trim after fixing.",
    ),
    MetricSpec("airborne_min", "Time in the air", "min", 0, 0.0, 0.0, "for context"),
)
BY_KEY = {m.key: m for m in METRICS}
MIN_AIR_S = 20.0
"""Signal metrics need this much time in the air; the score is always kept."""


def _in_air(flight: Flight, t: np.ndarray) -> np.ndarray:
    return in_air_mask(flight, t) & outside(t, impact_windows(flight))


def _measured(report: Report, check_id: str, unit: str) -> float | None:
    for f in report.findings:
        if f.check_id == check_id and f.measured is not None and f.unit == unit:
            return float(f.measured)
    return None


def flight_metrics(flight: Flight, report: Report) -> dict[str, float]:
    out: dict[str, float] = {}
    if report.score is not None:
        out["score"] = float(report.score)
    airborne = summarize(flight).airborne_s
    if airborne:
        out["airborne_min"] = airborne / 60

    if (summarize(flight).airborne_s or 0) < MIN_AIR_S:
        return {
            k: round(v, 4) for k, v in out.items()
        }  # too little flight for the signal metrics to mean anything
    vib: list[str] = [n for n in ("vibe.x", "vibe.y", "vibe.z") if n in flight.signals]
    if not vib and "vibe.metric" in flight.signals:
        vib = ["vibe.metric"]
    if vib:
        ref = flight.signals[vib[0]]
        worst = np.max(
            [np.interp(ref.t, flight.signals[n].t, flight.signals[n].values) for n in vib], 0
        )
        air = _in_air(flight, ref.t) & np.isfinite(worst)
        if air.sum() >= 10:
            out["vibration"] = float(np.percentile(worst[air], 95))

    if "vibe.clip" in flight.signals:
        clip = flight.signals["vibe.clip"]
        air = _in_air(flight, clip.t)
        if air.sum() >= 10:
            steps = np.diff(clip.values)
            minutes = float(np.sum(np.diff(clip.t)[air[1:]])) / 60
            if minutes > 0.5:
                out["clipping"] = float(np.sum(np.maximum(steps[air[1:]], 0))) / minutes

    cells = cell_count(flight)
    if cells and "bat.volt" in flight.signals:
        volt = flight.signals["bat.volt"]
        air = _in_air(flight, volt.t) & np.isfinite(volt.values)
        per_cell = volt.values / cells
        if (
            air.sum() >= 10
            and PLAUSIBLE_CELL_V[0] <= np.median(per_cell[air]) <= PLAUSIBLE_CELL_V[1]
        ):
            out["cell_min"] = float(np.percentile(per_cell[air], 1))
            fit = internal_resistance(flight, air)
            if fit is not None:
                out["battery_ir"] = fit[0] / cells

    motors = sorted(n for n in flight.signals if n.startswith("mot.") and n[4:].isdigit())
    if motors:
        ref = flight.signals[motors[0]]
        mean = np.mean(
            [np.interp(ref.t, flight.signals[m].t, flight.signals[m].values) for m in motors], 0
        )
        air = _in_air(flight, ref.t) & np.isfinite(mean)
        if air.sum() >= 10:
            out["motor_output"] = float(np.median(mean[air])) * 100

    ratios = [
        flight.signals[n]
        for n in ("ekf.vel", "ekf.pos", "ekf.hgt", "ekf.mag")
        if n in flight.signals
    ]
    peaks = []
    for s in ratios:
        air = _in_air(flight, s.t) & np.isfinite(s.values)
        if air.sum() >= 10:
            peaks.append(float(np.percentile(s.values[air], 99)))
    if peaks:
        out["ekf"] = max(peaks)

    for key, check, unit in (
        ("imbalance", "motor.imbalance", "%"),
        ("gyro_noise", "vibration.spectrum", "deg/s"),
        ("trim", "control.trim", "%"),
    ):
        v = _measured(report, check, unit)
        if v is not None:
            out[key] = v

    return {k: round(v, 4) for k, v in out.items() if np.isfinite(v)}
