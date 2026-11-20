"""Power and link checks: the autopilot's 5 V supply, battery reserve, RC link.

**power.supply** — signal ``power.5v`` (V; PX4 ``system_power.voltage5v_v``,
ArduPilot ``POWR.Vcc``), in the air. The flight controller and its sensors run
from this rail; below about 4.5 V boards start to brown out and reset. Healthy
boards in the calibration logs sit at 4.6-5.25 V (servos on the rail make it
swing by up to 0.35 V, which is not judged). Attention below ``attention``
(4.5 V), Problem below ``problem`` (4.3 V).

**battery.reserve** — signal ``bat.remaining`` (0-1), the flight controller's
own estimate. Judged at landing (the median of the last few seconds in the
air), and only when the estimate tracks the charge used: it must fall during
the flight (many logs carry a constant 0 or 1 because the capacity is not
configured), not faster than ``max_drop_per_min`` (half a pack per minute),
and never rise by ``max_rise`` (10 points) in the air. An estimate that breaks
the last two follows the voltage sag under load, as seen on real PX4 logs
(50% to 0% in 30 s of flight, back to 23% after landing).
Attention below ``attention`` (20% left), Problem below ``problem`` (10%).

**rc.link** — signal ``rc.lost`` (PX4 ``input_rc.rc_lost``), in the air.
Attention for any loss of the RC signal; Problem when it adds up to
``problem_s`` (1 s) or more. ArduPilot reports RC loss as a failsafe event,
which ``events.failsafes`` already covers.
"""

from __future__ import annotations

import numpy as np

from flightlens.checks._util import grade, held_windows, hold, in_air_mask
from flightlens.checks.base import register
from flightlens.intervals import format_time
from flightlens.model import CheckConfig, Finding, Flight, Severity
from flightlens.summary import summarize


@register
class PowerSupply:
    id = "power.supply"
    title = "Autopilot power supply"
    version = "1.0.0"
    required_signals = ("power.5v",)
    defaults = {"attention": 4.5, "problem": 4.3, "min_duration_s": 0.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        sig = flight.signal("power.5v")
        usable = in_air_mask(flight, sig.t) & np.isfinite(sig.values)
        if usable.sum() < 2:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Power supply not evaluated",
                    "No 5 V rail measurements while in the air.",
                    evidence_signals=("power.5v",),
                )
            ]
        b = grade(
            sig.t,
            sig.values,
            usable,
            config["attention"],
            config["problem"],
            above=False,
            min_duration=config["min_duration_s"],
        )
        lo, hi = float(sig.values[usable].min()), float(sig.values[usable].max())
        if b is None:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Power supply stable",
                    f"The 5 V rail stayed between {lo:.2f} and {hi:.2f} V in the air.",
                    measured=lo,
                    threshold=config["attention"],
                    comparison="<",
                    unit="V",
                    evidence_signals=("power.5v",),
                )
            ]
        return [
            Finding(
                self.id,
                b.severity,
                "Autopilot supply voltage dropped",
                f"The flight controller's 5 V rail fell to {b.worst:.2f} V in the air "
                f"(at {format_time(b.start)}). Below about 4.5 V the board and its sensors can "
                "brown out and reset mid-flight.",
                recommendation=(
                    "Check the power module or BEC and its wiring; move servos and other "
                    "high-current peripherals to a separate supply."
                ),
                measured=b.worst,
                threshold=b.limit,
                comparison="<",
                unit="V",
                start=b.start,
                end=b.end,
                evidence_signals=("power.5v",),
            )
        ]


@register
class BatteryReserve:
    id = "battery.reserve"
    title = "Battery reserve at landing"
    version = "1.0.0"
    required_signals = ("bat.remaining",)
    defaults = {
        "attention": 0.2,
        "problem": 0.1,
        "min_drop": 0.05,
        "max_drop_per_min": 0.5,
        "max_rise": 0.1,
        "landing_window_s": 5.0,
    }

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        sig = flight.signal("bat.remaining")
        air = in_air_mask(flight, sig.t) & np.isfinite(sig.values)
        why = None
        if air.sum() < 5:
            why = "No battery estimate while in the air."
        else:
            v = sig.values[air]
            t_air = sig.t[air]
            # how fast it fell: until the lowest value is first reached
            minutes = max(float(t_air[int(np.argmin(v))] - t_air[0]), 1.0) / 60
            rise = float(np.max(v - np.minimum.accumulate(v)))
            if float(v.max()) - float(v.min()) < config["min_drop"]:
                why = (
                    f"The battery estimate stayed at {float(np.median(v)):.0%} all flight, so it "
                    "is not tracking the battery (capacity probably not configured)."
                )
            elif (float(v[0]) - float(v.min())) / minutes > config["max_drop_per_min"]:
                why = (
                    f"The battery estimate fell {float(v[0]) - float(v.min()):.0%} in "
                    f"{minutes:.1f} min of flight, faster than any pack discharges: it follows "
                    "the voltage sag under load, not the charge used."
                )
            elif rise > config["max_rise"]:
                why = (
                    f"The battery estimate rose by {rise:.0%} in the air, so it follows the "
                    "voltage, not the charge used."
                )
        if why:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Battery reserve not evaluated",
                    why,
                    recommendation="Set the battery capacity (PX4 BAT1_CAPACITY, ArduPilot "
                    "BATT_CAPACITY) so the flight controller can estimate what is left.",
                    evidence_signals=("bat.remaining",),
                )
            ]
        sorties = summarize(flight).sorties
        t_air = sig.t[air]
        landing = sorties[-1]["end"] if sorties else float(t_air[-1])
        near = air & (sig.t >= landing - config["landing_window_s"]) & (sig.t <= landing)
        if near.sum() == 0:
            near = air & (sig.t <= landing)
        left = float(np.median(sig.values[near]))
        if left >= config["attention"]:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "Landed with reserve",
                    f"About {left:.0%} of the battery was left at landing.",
                    measured=left * 100,
                    threshold=config["attention"] * 100,
                    comparison="<",
                    unit="%",
                    evidence_signals=("bat.remaining",),
                )
            ]
        sev = Severity.PROBLEM if left < config["problem"] else Severity.ATTENTION
        return [
            Finding(
                self.id,
                sev,
                "Landed with little battery left",
                f"The flight controller estimated only {left:.0%} of the battery left at "
                f"landing ({format_time(landing)}). Flying the pack this low shortens its life "
                "and leaves no margin for a go-around or a headwind.",
                recommendation=(
                    "Land earlier, or set the low-battery warning and failsafe "
                    "(PX4 BAT_LOW_THR / BAT_CRIT_THR, ArduPilot BATT_LOW_MAH / BATT_LOW_VOLT) "
                    "so the aircraft returns with at least 20% left."
                ),
                measured=left * 100,
                threshold=(config["problem"] if sev is Severity.PROBLEM else config["attention"])
                * 100,
                comparison="<",
                unit="%",
                start=landing,
                end=landing,
                evidence_signals=("bat.remaining", "bat.volt")
                if flight.has("bat.volt")
                else ("bat.remaining",),
            )
        ]


@register
class RcLink:
    id = "rc.link"
    title = "RC link"
    version = "1.0.0"
    required_signals = ("rc.lost",)
    defaults = {"problem_s": 1.0}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        sig = flight.signal("rc.lost")
        grid = sig.t
        air = in_air_mask(flight, grid)
        if air.sum() < 2:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "RC link not evaluated",
                    "No RC status while in the air.",
                    evidence_signals=("rc.lost",),
                )
            ]
        lost = air & (hold(sig, grid) > 0.5)
        windows = held_windows(grid, lost, 0.0, merge_gap=0.5)
        if not windows:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "RC link held",
                    "The RC receiver never reported a lost signal in the air.",
                    evidence_signals=("rc.lost",),
                )
            ]
        # a single sample marks a loss of at least one logging interval
        dt = float(np.median(np.diff(grid))) if grid.size > 1 else 0.0
        total = sum(max(b - a, dt) for a, b in windows)
        sev = Severity.PROBLEM if total >= config["problem_s"] else Severity.ATTENTION
        return [
            Finding(
                self.id,
                sev,
                "RC signal lost in flight",
                f"The RC receiver lost the transmitter {len(windows)} time(s) in the air, "
                f"{total:.1f} s in total, first at {format_time(windows[0][0])}.",
                recommendation=(
                    "Check receiver antennas (orientation, damage, distance from carbon and "
                    "power wiring), transmitter power and range; review the RC loss failsafe."
                ),
                measured=total,
                threshold=config["problem_s"],
                unit="s",
                start=windows[0][0],
                end=windows[-1][1],
                evidence_signals=("rc.lost", "rc.rssi") if flight.has("rc.rssi") else ("rc.lost",),
            )
        ]
