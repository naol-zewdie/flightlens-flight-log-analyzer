"""Barometer: drift over the flight, and two barometers that disagree.

**baro.health** — signal ``baro.alt`` (PX4 ``vehicle_air_data.baro_alt_meter``,
ArduPilot ``BARO.Alt``).

* *Drift*: the barometric height at rest just before takeoff and just after
  landing should differ by as much as the ground does. The ground's change is
  taken from GPS altitude averaged at rest, where GPS is reliable (in flight it
  lags and drifts: on real crashes the barometer read 0 m on the ground while
  GPS still said 24 m, so barometer and GPS are *not* compared in the air).
  Attention above ``drift_attention`` (4 m), Problem above ``drift_problem``
  (8 m). Real flights of up to 11 minutes drift 1.7 m at most.
* *Two barometers* (ArduPilot ``BARO`` instance 1): after removing their fixed
  offset, Attention when they disagree by more than ``dual_attention`` (5 m),
  Problem above ``dual_problem`` (10 m), held ``dual_min_s`` (3 s) in the air.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import grade, in_air_mask, resample
from flightlens.checks.base import register
from flightlens.intervals import format_time
from flightlens.model import CheckConfig, Finding, Flight, Severity, Signal
from flightlens.summary import summarize


def _rest(sig: Signal, a: float, b: float, good: Signal | None = None) -> float | None:
    sel = (sig.t >= a) & (sig.t <= b) & np.isfinite(sig.values)
    if good is not None:
        with np.errstate(invalid="ignore"):
            sel &= resample(good, sig.t, 2.0) >= 3  # a 3D GPS fix
    return float(np.median(sig.values[sel])) if sel.sum() >= 3 else None


@register
class BaroHealth:
    id = "baro.health"
    title = "Barometer"
    version = "1.0.0"
    required_signals = ("baro.alt",)
    defaults = {
        "drift_attention": 4.0,
        "drift_problem": 8.0,
        "rest_window_s": 5.0,
        "dual_attention": 5.0,
        "dual_problem": 10.0,
        "dual_min_s": 3.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        findings: list[Finding] = []
        notes: list[str] = []
        drift = self._drift(flight, config)
        if isinstance(drift, Finding):
            findings.append(drift)
        elif drift is not None:
            notes.append(drift)
        dual = self._dual(flight, config)
        if isinstance(dual, Finding):
            findings.append(dual)
        elif dual is not None:
            notes.append(dual)
        if findings:
            return findings
        if not notes:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Barometer not evaluated",
                    "Drift needs the barometer and a GPS fix at rest before takeoff and after "
                    "landing; the log has no such ground periods, and only one barometer.",
                    evidence_signals=("baro.alt",),
                )
            ]
        return [
            Finding(
                self.id,
                Severity.OK,
                "Barometer consistent",
                " ".join(notes),
                evidence_signals=("baro.alt", "gps.alt"),
            )
        ]

    def _drift(self, flight: Flight, config: CheckConfig) -> Finding | str | None:
        if "gps.alt" not in flight.signals:
            return None
        baro, gps = flight.signal("baro.alt"), flight.signal("gps.alt")
        fix = flight.signals.get("gps.fix")
        w = config["rest_window_s"]
        worst: tuple[float, float, float] | None = None  # drift, takeoff, landing
        sorties = summarize(flight).sorties
        for k, s in enumerate(sorties):
            a, z = s["start"], s["end"]
            # rest windows stop short of the previous and next sortie (touch-and-go,
            # quick relaunch), so they never measure the aircraft in flight
            before = max(a - 1 - w, sorties[k - 1]["end"] + 2 if k else -np.inf)
            after = min(z + 2 + w, sorties[k + 1]["start"] - 1 if k + 1 < len(sorties) else np.inf)
            values = (
                _rest(baro, before, a - 1),
                _rest(baro, z + 2, after),
                _rest(gps, before, a - 1, fix),
                _rest(gps, z + 2, after, fix),
            )
            if any(v is None for v in values):
                continue
            b0, b1, g0, g1 = (float(v) for v in values)  # type: ignore[arg-type]
            d = (b1 - b0) - (g1 - g0)
            if worst is None or abs(d) > abs(worst[0]):
                worst = (d, a, z)
        if worst is None:
            return None
        d, a, z = worst
        size = abs(d)
        if size <= config["drift_attention"]:
            return f"Barometric height drifted {d:+.1f} m between takeoff and landing."
        problem = size > config["drift_problem"]
        return Finding(
            self.id,
            Severity.PROBLEM if problem else Severity.ATTENTION,
            "Barometer drifted",
            f"Between takeoff ({format_time(a)}) and landing ({format_time(z)}) the barometric "
            f"height changed {d:+.1f} m more than the ground did (GPS at rest). The estimated "
            "height drifts the same way, so altitude holds and landings are off by that much.",
            recommendation=(
                "Shield the barometer from light and airflow with open-cell foam, keep it away "
                "from heat sources, and let the board warm up before takeoff."
            ),
            measured=size,
            threshold=config["drift_problem"] if problem else config["drift_attention"],
            unit="m",
            evidence_signals=("baro.alt", "gps.alt"),
        )

    def _dual(self, flight: Flight, config: CheckConfig) -> Finding | str | None:
        if "baro.alt2" not in flight.signals:
            return None
        b1 = flight.signal("baro.alt")
        t = b1.t
        other = resample(flight.signal("baro.alt2"), t, 1.0)
        usable = in_air_mask(flight, t) & np.isfinite(b1.values) & np.isfinite(other)
        if usable.sum() < 10:
            return None
        diff = b1.values - other
        dev = np.abs(diff - np.median(diff[usable]))
        b = grade(
            t,
            dev,
            usable,
            config["dual_attention"],
            config["dual_problem"],
            min_duration=config["dual_min_s"],
        )
        if b is None:
            return f"The two barometers agreed within {float(dev[usable].max()):.1f} m."
        return Finding(
            self.id,
            b.severity,
            "Barometers disagree",
            f"The two barometers differed by {b.worst:.1f} m in the air (at "
            f"{format_time(b.start)}), after removing their usual offset. One of them is "
            "affected by airflow, light or heat, or is failing.",
            recommendation="Check how each barometer is covered and placed; replace a failing one.",
            measured=b.worst,
            threshold=b.limit,
            unit="m",
            start=b.start,
            end=b.end,
            evidence_signals=("baro.alt", "baro.alt2"),
        )
