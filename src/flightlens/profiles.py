"""Threshold profiles: named, versioned threshold overrides for one aircraft.

The checks' defaults are community starting values. A profile replaces some of
them for one aircraft, either written by hand (a YAML file or the web app) or
*learned* from that aircraft's own earlier flights. Every report records the
profile it used (name, source and a digest of the values), so a verdict can
always be traced back to the thresholds that produced it.

YAML format::

    name: Quad-7
    description: heavy-lift frame, stiffer mounts
    checks:
      vibration.level: {attention: 25, problem: 45}
      battery.sag: {attention: 3.55}

Learning is deliberately narrow. Only *attention* thresholds of three checks
are learned, each from the per-flight metric that measures the same quantity
(``flightlens.metrics``). The value is the aircraft's median plus the same
margin that drift alerts use (``fleet.drift_limit``), and it is clamped: never
as loose as the default *problem* threshold and never tighter than half way
to zero from the default. Problem thresholds are never learned.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from flightlens.checks.base import Check, all_checks
from flightlens.metrics import BY_KEY

MIN_LEARN_FLIGHTS = 4
"""Eligible flights (analyzed, not a Problem verdict) needed to learn a profile."""
LEARN_FROM_VERDICTS = ("healthy", "attention")
SOURCES = ("file", "user", "learned")
NAME_MAX = 60


class ProfileError(ValueError):
    """A profile is malformed, refers to unknown settings, or cannot be learned."""


@dataclass(frozen=True)
class Profile:
    name: str
    overrides: dict[str, dict[str, float]]
    source: str = "file"
    notes: str = ""
    basis: dict[str, Any] | None = None
    """For a learned profile: the flights and statistics each value came from."""

    @property
    def digest(self) -> str:
        """Short hash of the values; changes whenever any threshold changes."""
        canon = json.dumps(self.overrides, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:12]

    def provenance(self) -> dict[str, Any]:
        """What a report records about the profile it used."""
        return {
            "name": self.name,
            "source": self.source,
            "digest": self.digest,
            "overrides": self.overrides,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.provenance(), "notes": self.notes, "basis": self.basis}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Profile:
        return cls(
            name=str(data["name"]),
            overrides={k: dict(v) for k, v in (data.get("overrides") or {}).items()},
            source=str(data.get("source") or "user"),
            notes=str(data.get("notes") or ""),
            basis=data.get("basis"),
        )

    def to_yaml(self) -> str:
        doc: dict[str, Any] = {"name": self.name}
        if self.notes:
            doc["description"] = self.notes
        if self.source == "learned":
            doc["learned"] = True
        doc["checks"] = self.overrides
        if self.basis:
            doc["basis"] = self.basis
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _pairs(defaults: Mapping[str, float]) -> list[tuple[str, str]]:
    """(attention key, problem key) pairs of a check, e.g. ``px4_attention``."""
    out = []
    for key in defaults:
        if key.endswith("attention"):
            prob = key[: -len("attention")] + "problem"
            if prob in defaults:
                out.append((key, prob))
    return out


def validate_overrides(
    overrides: Any, checks: Sequence[Check] | None = None
) -> dict[str, dict[str, float]]:
    """Check and normalise threshold overrides; raises :class:`ProfileError`.

    Rejects unknown checks and settings, non-numbers, and an attention threshold
    that would be on the wrong side of the problem threshold.
    """
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise ProfileError("'checks' must map a check id to its settings")
    by_id = {c.id: c for c in (checks if checks is not None else all_checks())}
    out: dict[str, dict[str, float]] = {}
    for check_id, settings in overrides.items():
        check = by_id.get(str(check_id))
        if check is None:
            raise ProfileError(f"unknown check {check_id!r} (see 'flightlens checks')")
        if not isinstance(settings, Mapping) or not settings:
            raise ProfileError(f"{check_id}: settings must be a non-empty mapping")
        clean: dict[str, float] = {}
        for key, value in settings.items():
            if key not in check.defaults:
                known = ", ".join(sorted(check.defaults))
                raise ProfileError(f"{check_id}: unknown setting {key!r} (known: {known})")
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ProfileError(f"{check_id}.{key}: {value!r} is not a number")
            if value != value or value in (float("inf"), float("-inf")):
                raise ProfileError(f"{check_id}.{key}: must be a finite number")
            clean[str(key)] = float(value)
        effective = {**check.defaults, **clean}
        for att, prob in _pairs(check.defaults):
            higher_is_worse = check.defaults[prob] >= check.defaults[att]
            a, p = effective[att], effective[prob]
            if (a > p) if higher_is_worse else (a < p):
                side = "above" if higher_is_worse else "below"
                raise ProfileError(f"{check_id}: {att} ({a:g}) must not be {side} {prob} ({p:g})")
        out[str(check_id)] = clean
    return out


def parse_profile(
    data: Any, source: str = "file", checks: Sequence[Check] | None = None
) -> Profile:
    """Build a profile from the YAML document (or the same structure from the API)."""
    if not isinstance(data, Mapping):
        raise ProfileError("a profile must be a mapping with 'name' and 'checks'")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ProfileError("a profile needs a 'name'")
    if len(name) > NAME_MAX:
        raise ProfileError(f"the name is longer than {NAME_MAX} characters")
    if source not in SOURCES:
        raise ProfileError(f"unknown profile source {source!r}")
    if data.get("learned"):
        source = "learned"
    return Profile(
        name=name,
        overrides=validate_overrides(data.get("checks"), checks),
        source=source,
        notes=str(data.get("description") or data.get("notes") or ""),
        basis=data.get("basis") if isinstance(data.get("basis"), dict) else None,
    )


def load_profile(path: Path) -> Profile:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path.name} is not valid YAML: {exc}") from None
    return parse_profile(data, source="file")


# --- learning ------------------------------------------------------------------------


@dataclass(frozen=True)
class Learnable:
    metric: str
    """Key in ``flightlens.metrics``; same quantity as the threshold."""
    check_id: str
    keys: Mapping[str, str]
    """Attention key per log format (``dataflash``, ``ulog``)."""
    scale: float = 1.0
    """Metric units to threshold units (imbalance is stored in %, judged as a fraction)."""
    formats: tuple[str, ...] = field(default=("dataflash", "ulog"))


LEARNABLE: tuple[Learnable, ...] = (
    # p95 of the worst vibration axis in the air vs. a level held for a few seconds
    Learnable("vibration", "vibration.level", {"dataflash": "attention", "ulog": "px4_attention"}),
    Learnable(
        "imbalance",
        "motor.imbalance",
        {"dataflash": "attention", "ulog": "attention"},
        scale=0.01,
    ),
    # 1st percentile of volts per cell in the air vs. a sag held for a second
    Learnable("cell_min", "battery.sag", {"dataflash": "attention", "ulog": "attention"}),
)


def _clamp(check: Check, att_key: str, value: float) -> tuple[float, bool]:
    """Keep a learned attention threshold between half the default and the problem level."""
    prob_key = att_key[: -len("attention")] + "problem"
    att, prob = check.defaults[att_key], check.defaults[prob_key]
    gap = abs(prob - att)
    worse = 1.0 if prob >= att else -1.0
    tight = att - worse * min(gap, 0.5 * abs(att))  # strictest allowed
    loose = att + worse * 0.9 * gap  # stays clear of the problem threshold
    lo, hi = sorted((tight, loose))
    clamped = min(max(value, lo), hi)
    return clamped, clamped != value


def _metrics(row: Mapping[str, Any]) -> dict[str, float]:
    m = row.get("metrics")
    if isinstance(m, str):
        m = json.loads(m)
    return dict(m or {})


def eligible(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Flights a profile may be learned from: analyzed, and no Problem verdict."""
    return [
        r for r in rows if r.get("verdict") in LEARN_FROM_VERDICTS and r.get("metrics") is not None
    ]


def learn_profile(name: str, rows: Iterable[Mapping[str, Any]], notes: str = "") -> Profile:
    """Learn attention thresholds from one aircraft's flights (index rows with metrics)."""
    from flightlens.fleet import drift_limit

    use = eligible(rows)
    if len(use) < MIN_LEARN_FLIGHTS:
        raise ProfileError(
            f"Learning needs at least {MIN_LEARN_FLIGHTS} analyzed flights without a Problem "
            f"verdict; this aircraft has {len(use)}."
        )
    by_id = {c.id: c for c in all_checks()}
    overrides: dict[str, dict[str, float]] = {}
    learned: dict[str, Any] = {}
    for item in LEARNABLE:
        check = by_id[item.check_id]
        spec = BY_KEY[item.metric]
        for fmt in item.formats:
            values = [
                _metrics(r)[item.metric]
                for r in use
                if r.get("log_format") == fmt and item.metric in _metrics(r)
            ]
            if len(values) < MIN_LEARN_FLIGHTS:
                continue
            base, margin = drift_limit(spec, values)
            raw = (base + spec.worse * margin) * item.scale
            key = item.keys[fmt]
            value, clamped = _clamp(check, key, raw)
            value = round(value, 4)
            overrides.setdefault(item.check_id, {})[key] = value
            learned[f"{item.check_id}.{key}"] = {
                "metric": item.metric,
                "flights": len(values),
                "median": round(base * item.scale, 4),
                "margin": round(margin * item.scale, 4),
                "default": check.defaults[key],
                "clamped": clamped,
            }
    if not overrides:
        raise ProfileError(
            "None of the learnable metrics (vibration, motor imbalance, lowest cell voltage) "
            f"was measured on at least {MIN_LEARN_FLIGHTS} of these flights."
        )
    return Profile(
        name=name,
        overrides=validate_overrides(overrides),
        source="learned",
        notes=notes,
        basis={
            "flights": len(use),
            "flight_ids": sorted(str(r.get("id")) for r in use if r.get("id")),
            "values": learned,
        },
    )
