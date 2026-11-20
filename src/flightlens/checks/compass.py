"""Compass interference: the magnetic field changes with motor current.

Signals: ``mag.x/y/z`` (gauss) and ``bat.curr`` (A), in the air.

The Earth's field is constant over a flight, so its measured strength should
be too. Current in nearby power wires and motors adds a field of its own, which
grows with the current: the estimator then sees a heading error that changes
with throttle (toilet-bowling, drifting heading, EKF compass failures).

The field strength is fitted against current. As in ArduPilot's compassmot,
the interference is the field the current adds at the highest current of the
flight, relative to the Earth's field (the fitted strength at zero current).
It only sees the part of the interference that changes the field's strength
(not its direction), so it is a lower bound, judged against compassmot's
limits: Attention above ``attention`` (30%), Problem above ``problem`` (60%).
It needs at least ``min_current_span`` (3 A) between low and high current in
the air.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import impact_windows, in_air_mask, outside, resample
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity

MIN_SAMPLES = 50


@register
class CompassInterference:
    id = "compass.interference"
    title = "Compass interference"
    version = "1.0.0"
    required_signals = ("mag.x", "mag.y", "mag.z", "bat.curr")
    defaults = {"attention": 30.0, "problem": 60.0, "min_current_span": 3.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        mx, my, mz = (flight.signal(f"mag.{a}") for a in "xyz")
        t = mx.t
        field = np.sqrt(mx.values**2 + resample(my, t) ** 2 + resample(mz, t) ** 2)
        current = resample(flight.signal("bat.curr"), t, max_gap=2.0)
        ok = in_air_mask(flight, t) & outside(t, impact_windows(flight))
        ok &= np.isfinite(field) & np.isfinite(current) & (field > 0)
        evidence = ("mag.x", "mag.y", "mag.z", "bat.curr")
        if ok.sum() < MIN_SAMPLES:
            return [self._skip("Too few compass and current samples in the air.", evidence)]
        b, i = field[ok], current[ok]
        lo, hi = np.percentile(i, [5, 95])
        if hi - lo < config["min_current_span"]:
            return [
                self._skip(
                    f"The current only varied between {lo:.1f} and {hi:.1f} A, too little to "
                    "see whether the compass follows it.",
                    evidence,
                )
            ]
        slope, offset = np.polyfit(i, b, 1)
        earth = float(offset)
        if earth < 0.2 * float(np.median(b)):  # the extrapolation to 0 A is not credible
            earth = float(np.median(b))
        added = abs(float(slope)) * hi
        pct = added / earth * 100
        r = float(np.corrcoef(i, b)[0, 1]) if np.std(b) > 0 else 0.0
        detail = (
            f"At {hi:.0f} A the power wiring adds about {added * 1000:.0f} mG, {pct:.0f}% of the "
            f"Earth's field here ({earth * 1000:.0f} mG); field strength against current "
            f"correlates {r:+.2f}."
        )
        if pct <= config["attention"]:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Compass unaffected by current",
                    detail,
                    measured=pct,
                    threshold=config["attention"],
                    unit="%",
                    evidence_signals=evidence,
                )
            ]
        sev = Severity.PROBLEM if pct > config["problem"] else Severity.ATTENTION
        return [
            Finding(
                self.id,
                sev,
                "Compass affected by motor current",
                detail + " The compass sees the field of the power wiring, so the heading the "
                "estimator uses shifts with throttle.",
                recommendation=(
                    "Move the compass (or the GPS mast that carries it) away from power wires, "
                    "ESCs and the battery; twist power leads; on ArduPilot run compassmot, on "
                    "PX4 calibrate the power compensation (CAL_MAGn_XCOMP...)."
                ),
                measured=pct,
                threshold=config["problem"] if sev is Severity.PROBLEM else config["attention"],
                unit="%",
                evidence_signals=evidence,
            )
        ]

    def _skip(self, why: str, evidence: tuple[str, ...]) -> Finding:
        return Finding(
            self.id,
            Severity.NOT_EVALUATED,
            "Compass interference not evaluated",
            why,
            evidence_signals=evidence,
        )
