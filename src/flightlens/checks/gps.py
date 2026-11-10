"""GPS quality check.

Signals: ``gps.nsats``, ``gps.hdop``, ``gps.fix`` (fix type; 3 = 3D fix).

Method: while armed, the worst condition held for at least 5 s.
Attention: fewer than 8 satellites or HDOP above 1.4.
Problem: fewer than 6 satellites, HDOP above 2.5, or loss of 3D fix for 2 s.

If the receiver never had a 3D fix anywhere in the log, the flight most likely
did not use GPS (indoor, vision or simulation), so the check is not evaluated
instead of raising a false alarm.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import held_windows
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity

_RECOMMENDATION = (
    "Check the GPS antenna placement and its view of the sky, and look for interference "
    "from video or telemetry transmitters."
)


@register
class GpsQuality:
    id = "gps.quality"
    title = "GPS quality"
    version = "1.0.0"
    required_signals = ("gps.nsats", "gps.hdop", "gps.fix")
    defaults = {
        "sats_attention": 8.0,
        "sats_problem": 6.0,
        "hdop_attention": 1.4,
        "hdop_problem": 2.5,
        "min_duration_s": 5.0,
        "fix_loss_s": 2.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        fix = flight.signal("gps.fix")
        if not np.any(fix.values >= 3):
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "GPS not evaluated",
                    "The receiver never had a 3D fix in this log, so the flight most likely "
                    "did not rely on GPS.",
                    evidence_signals=("gps.fix",),
                )
            ]
        t = fix.t
        armed = flight.armed_mask(t)
        if armed.sum() < 2:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "GPS not evaluated",
                    "No GPS samples while armed.",
                    evidence_signals=self.required_signals,
                )
            ]
        # All three come from the same topic, so they share time stamps.
        sats = flight.signal("gps.nsats").values
        hdop = flight.signal("gps.hdop").values
        if sats.shape != t.shape or hdop.shape != t.shape:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "GPS not evaluated",
                    "GPS fields come from different sources and cannot be compared.",
                )
            ]

        dur = config["min_duration_s"]
        candidates: list[Finding] = []

        lost = held_windows(t, armed & (fix.values < 3), config["fix_loss_s"])
        if lost:
            a, b = max(lost, key=lambda w: w[1] - w[0])
            candidates.append(
                self._finding(
                    Severity.PROBLEM,
                    "GPS 3D fix lost",
                    f"The receiver lost its 3D fix for {b - a:.0f} s while armed.",
                    None,
                    None,
                    "",
                    a,
                    b,
                    "<",
                )
            )

        for sev, s_lim, h_lim in (
            (Severity.PROBLEM, config["sats_problem"], config["hdop_problem"]),
            (Severity.ATTENTION, config["sats_attention"], config["hdop_attention"]),
        ):
            ok_fix = armed & (fix.values >= 3)
            low = held_windows(t, ok_fix & (sats < s_lim), dur)
            if low:
                a, b = low[0]
                sel = (t >= a) & (t <= b)
                worst = float(np.nanmin(sats[sel]))
                candidates.append(
                    self._finding(
                        sev,
                        "Few GPS satellites",
                        f"Only {worst:.0f} satellites were used for {b - a:.0f} s.",
                        worst,
                        s_lim,
                        "satellites",
                        a,
                        b,
                        "<",
                    )
                )
            high = held_windows(t, ok_fix & (hdop > h_lim), dur)
            if high:
                a, b = high[0]
                sel = (t >= a) & (t <= b)
                worst = float(np.nanmax(hdop[sel]))
                candidates.append(
                    self._finding(
                        sev,
                        "GPS precision dropped",
                        f"HDOP rose to {worst:.1f} for {b - a:.0f} s, so position was less "
                        "precise.",
                        worst,
                        h_lim,
                        "",
                        a,
                        b,
                        ">",
                    )
                )
            if candidates:
                break

        if candidates:
            # One finding per kind, most serious first.
            seen: dict[str, Finding] = {}
            for f in sorted(candidates, key=lambda f: -f.severity.rank):
                seen.setdefault(f.title, f)
            return list(seen.values())

        s_min = float(np.nanmin(sats[armed]))
        h_max = float(np.nanmax(hdop[armed]))
        return [
            Finding(
                self.id,
                Severity.OK,
                "GPS healthy",
                f"At least {s_min:.0f} satellites and HDOP at most {h_max:.1f} while armed.",
                measured=s_min,
                unit="satellites",
                evidence_signals=self.required_signals,
            )
        ]

    def _finding(
        self,
        severity: Severity,
        title: str,
        explanation: str,
        measured: float | None,
        threshold: float | None,
        unit: str,
        start: float,
        end: float,
        comparison: str,
    ) -> Finding:
        return Finding(
            check_id=self.id,
            severity=severity,
            title=title,
            explanation=explanation,
            recommendation=_RECOMMENDATION,
            measured=measured,
            threshold=threshold,
            comparison=comparison,
            unit=unit,
            start=start,
            end=end,
            evidence_signals=self.required_signals,
        )
