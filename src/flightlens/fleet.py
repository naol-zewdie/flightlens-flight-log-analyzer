"""Aircraft and their health over time.

Flights are grouped into aircraft by the user's label, or else by the
autopilot board serial. For each metric (``flightlens.metrics``) a flight is
compared with the median of the same aircraft's earlier flights, in flight
order. It is flagged as drift when it is worse by more than the metric's
relative and absolute minimum and by more than 3 robust standard deviations
of the earlier flights (1.4826 times the median absolute deviation), so an
aircraft that always varies a lot is not flagged for its usual scatter.
At least ``MIN_HISTORY`` earlier flights are needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from flightlens.metrics import BY_KEY, METRICS, MetricSpec

MIN_HISTORY = 3


def aircraft_key(row: dict[str, Any]) -> str | None:
    if row.get("aircraft"):
        return f"label:{row['aircraft']}"
    if row.get("serial"):
        return f"sn:{row['serial']}"
    return None


def flight_time(row: dict[str, Any]) -> str:
    """Sort key: the log's own UTC start, else the upload time."""
    return str(row.get("start_time") or f"~{row.get('uploaded_at', 0):020.3f}")


@dataclass
class Point:
    flight_id: str
    when: str | None
    value: float
    baseline: float | None = None
    flagged: bool = False


@dataclass
class Series:
    spec: MetricSpec
    points: list[Point] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        s = self.spec
        return {
            "key": s.key,
            "label": s.label,
            "unit": s.unit,
            "worse": s.worse,
            "meaning": s.meaning,
            "points": [p.__dict__ for p in self.points],
        }


def assess(spec: MetricSpec, values: list[float]) -> list[tuple[float | None, bool]]:
    """(baseline, flagged) for each value, against the values before it."""
    out: list[tuple[float | None, bool]] = []
    for i, v in enumerate(values):
        history = values[:i]
        if spec.worse == 0 or len(history) < MIN_HISTORY:
            out.append((median(history) if history else None, False))
            continue
        base = median(history)
        spread = 1.4826 * median(abs(h - base) for h in history)
        worse_by = (v - base) * spec.worse
        limit = max(spec.rel * abs(base), spec.abs_min, 3 * spread)
        out.append((base, worse_by > limit))
    return out


def series(rows: list[dict[str, Any]]) -> list[Series]:
    """Metric series for one aircraft's flights (rows in any order)."""
    rows = sorted(rows, key=flight_time)
    metrics = [json.loads(r["metrics"]) if r.get("metrics") else {} for r in rows]
    out = []
    for spec in METRICS:
        pts = [
            Point(r["id"], r.get("start_time"), float(m[spec.key]))
            for r, m in zip(rows, metrics, strict=True)
            if spec.key in m
        ]
        if not pts:
            continue
        for p, (base, flag) in zip(pts, assess(spec, [p.value for p in pts]), strict=True):
            p.baseline, p.flagged = base, flag
        out.append(Series(spec, pts))
    return out


def alerts(all_series: list[Series], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drift on each metric's most recent flight: the aircraft's current state."""
    names = {r["id"]: r["filename"] for r in rows}
    out = []
    for s in all_series:
        last = s.points[-1]
        if not last.flagged or last.baseline is None:
            continue
        spec = s.spec
        n = len(s.points) - 1
        unit = f" {spec.unit}" if spec.unit else ""
        up = last.value > last.baseline
        base = f"{last.baseline:.3g}{unit}"
        pct = abs(last.value - last.baseline) / abs(last.baseline) * 100 if last.baseline else None
        if pct is not None and pct <= 200:
            change = f"{pct:.0f}% {'above' if up else 'below'} this aircraft's usual {base}"
        else:  # a percentage of a near-zero baseline means nothing
            change = f"{'up' if up else 'down'} from this aircraft's usual {base}"
        out.append(
            {
                "metric": spec.key,
                "title": f"{spec.label} has drifted",
                "text": (
                    f"{spec.label} was {last.value:.3g}{unit} on the latest flight "
                    f"({names.get(last.flight_id, last.flight_id)}), {change} "
                    f"(median of {n} earlier flights). {spec.cause}"
                ),
                "flight_id": last.flight_id,
                "value": last.value,
                "baseline": last.baseline,
            }
        )
    return out


def summarize_aircraft(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=flight_time)
    last = rows[-1]
    s = series(rows)
    serials = sorted({r["serial"] for r in rows if r.get("serial")})
    return {
        "key": key,
        "label": last.get("aircraft") or None,
        "serials": serials,
        "hardware": last.get("hardware"),
        "vehicle_type": last.get("vehicle_type"),
        "flights": len(rows),
        "first": rows[0].get("start_time"),
        "last": last.get("start_time"),
        "latest_verdict": last.get("verdict"),
        "latest_score": last.get("score"),
        "airborne_min": round(
            sum(json.loads(r["metrics"]).get("airborne_min", 0) for r in rows if r.get("metrics")),
            1,
        ),
        "alerts": alerts(s, rows),
    }


__all__ = ["BY_KEY", "aircraft_key", "alerts", "assess", "series", "summarize_aircraft"]
