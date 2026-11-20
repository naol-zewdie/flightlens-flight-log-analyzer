"""Battery sag check.

Signals: ``bat.volt`` and the cell count (``bat.cells``, or the BAT1_N_CELLS /
BAT_N_CELLS parameter on older PX4).

Method: per-cell voltage while armed, held for at least 1 s so single-sample
dips are ignored. Attention below 3.5 V/cell, Problem below 3.3 V/cell
(LiPo/Li-ion starting values; Li-ion packs often run lower by design, so tune
per aircraft profile).
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import armed_usable, grade, impact_windows, outside
from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity

CELL_PARAMS = ("BAT1_N_CELLS", "BAT_N_CELLS")
PLAUSIBLE_CELL_V = (2.5, 4.5)


@register
class BatterySag:
    id = "battery.sag"
    title = "Battery voltage"
    version = "1.2.0"
    required_signals = ("bat.volt",)
    defaults = {"attention": 3.5, "problem": 3.3, "min_duration_s": 1.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        volt = flight.signal("bat.volt")
        cells = cell_count(flight)
        if cells is None:
            return [self._not_evaluated("The battery cell count is unknown.")]

        per_cell = volt.values / cells
        # A crash or hard touchdown can briefly collapse the voltage; not a battery fault.
        usable = armed_usable(flight, volt) & outside(volt.t, impact_windows(flight))
        if usable.sum() < 2:
            return [self._not_evaluated("No battery voltage samples while armed.")]
        median = float(np.median(per_cell[usable]))
        if not PLAUSIBLE_CELL_V[0] <= median <= PLAUSIBLE_CELL_V[1]:
            return [
                self._not_evaluated(
                    f"Voltage per cell ({median:.2f} V with {cells} cells) is implausible; "
                    "the cell count or voltage calibration is probably wrong."
                )
            ]

        b = grade(
            volt.t,
            per_cell,
            usable,
            config["attention"],
            config["problem"],
            above=False,
            min_duration=config["min_duration_s"],
        )
        if b is None:
            low = float(np.min(per_cell[usable]))
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Battery healthy",
                    f"Lowest {low:.2f} V per cell under load ({cells} cells).",
                    measured=low,
                    threshold=config["attention"],
                    comparison="<",
                    unit="V/cell",
                    evidence_signals=("bat.volt",),
                )
            ]
        return [
            Finding(
                check_id=self.id,
                severity=b.severity,
                title="Battery voltage sagged",
                explanation=(
                    f"The battery dropped to {b.worst:.2f} V per cell ({cells} cells) "
                    f"for {b.end - b.start:.0f} s while armed."
                    + _resistance_text(flight, cells, usable)
                ),
                recommendation=(
                    "Land earlier, check the battery's health and internal resistance, "
                    "and review the low-battery failsafe voltage."
                ),
                measured=b.worst,
                threshold=b.limit,
                comparison="<",
                unit="V/cell",
                start=b.start,
                end=b.end,
                evidence_signals=("bat.volt", "bat.curr")
                if flight.has("bat.curr")
                else ("bat.volt",),
            )
        ]

    def _not_evaluated(self, why: str) -> Finding:
        return Finding(
            self.id,
            Severity.NOT_EVALUATED,
            "Battery not evaluated",
            why,
            evidence_signals=("bat.volt",),
        )


def internal_resistance(flight: Flight, usable: np.ndarray) -> tuple[float, float] | None:
    """(pack resistance in mΩ, highest current in A) from a least-squares fit of
    voltage against current; None without a real spread of current."""
    if "bat.curr" not in flight.signals:
        return None
    volt = flight.signal("bat.volt")
    curr = np.interp(volt.t, flight.signal("bat.curr").t, flight.signal("bat.curr").values)
    sel = usable & np.isfinite(curr) & np.isfinite(volt.values)
    if sel.sum() < 20 or np.ptp(curr[sel]) < 10:
        return None
    r_mohm = -float(np.polyfit(curr[sel], volt.values[sel], 1)[0]) * 1000
    if r_mohm <= 0:
        return None
    return r_mohm, float(np.nanmax(curr[sel]))


def _resistance_text(flight: Flight, cells: int, usable: np.ndarray) -> str:
    fit = internal_resistance(flight, usable)
    if fit is None:
        return ""
    r_mohm, i_max = fit
    return (
        f" Voltage fell about {r_mohm / 1000 * i_max:.1f} V at {i_max:.0f} A: an internal "
        f"resistance of about {r_mohm:.0f} mΩ ({r_mohm / cells:.0f} mΩ per cell). A healthy "
        "pack is typically under 5 mΩ per cell; much higher means a worn or undersized "
        "battery, or poor connectors."
    )


def cell_count(flight: Flight) -> int | None:
    if "bat.cells" in flight.signals:
        vals = flight.signals["bat.cells"].values
        vals = vals[np.isfinite(vals) & (vals > 0)]
        if vals.size:
            return int(np.median(vals))
    for p in CELL_PARAMS:
        n = flight.parameters.get(p)
        if n and n > 0:
            return int(n)
    return None
