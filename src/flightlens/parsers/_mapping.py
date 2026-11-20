"""Shared, format-independent parts of the log adapters.

Each adapter (PX4 ULog, ArduPilot DataFlash) has a YAML table that maps its
own topic or message fields to FlightLens signal names. The table is applied
here, so both formats treat ``scale``, ``min_valid``, ``max_valid``,
``zero_is_missing`` and ``instance`` identically.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from flightlens.model import Event, FloatArray, ParseIssue, Segment

MAPPINGS_DIR = Path(__file__).parent / "mappings"

TopicKey = tuple[str, int]
Lookup = Callable[[str, int, str], FloatArray | None]
"""(topic, instance, field) -> raw values, or None if the log lacks them."""
Add = Callable[[str, str, TopicKey, FloatArray, str], None]
"""(signal name, unit, topic key, values, source description) -> None."""


@lru_cache(maxsize=4)
def load_mapping(name: str) -> dict[str, Any]:
    with (MAPPINGS_DIR / f"{name}.yaml").open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    return data


def mapping_topics(mapping: dict[str, Any]) -> set[str]:
    topics = {src["topic"] for spec in mapping["signals"] for src in spec["sources"]}
    topics.update(mapping.get("computed_topics", []))
    return topics


def apply_mapping(mapping: dict[str, Any], lookup: Lookup, add: Add) -> None:
    """Add every table-driven signal; for each, the first source present wins."""
    for spec in mapping["signals"]:
        for src in spec["sources"]:
            key = (src["topic"], int(src.get("instance", 0)))
            raw = lookup(key[0], key[1], src["field"])
            if raw is None:
                continue
            raw = raw.astype(np.float64)
            if src.get("zero_is_missing"):
                raw = np.where(raw == 0, np.nan, raw)
            if "min_valid" in src:
                raw = np.where(raw < src["min_valid"], np.nan, raw)
            if "max_valid" in src:
                raw = np.where(raw > src["max_valid"], np.nan, raw)
            raw = raw * float(src.get("scale", 1.0)) + 0.0  # + 0.0 turns -0.0 into 0.0
            add(spec["name"], spec["unit"], key, raw, f"{key[0]}[{key[1]}].{src['field']}")
            break


def arming_quality(
    events: list[Event],
    segments: list[Segment],
    arming_known: bool,
    quality: list[ParseIssue],
) -> None:
    """Quality issues every format reports the same way."""
    ends_armed = next((e for e in events if e.text == "Log ends while armed"), None)
    if ends_armed is not None:
        quality.append(
            ParseIssue(
                "ends_armed",
                "The log ends while the aircraft is still armed: the file may be truncated, "
                "or logging stopped in flight (power loss or crash)",
                ends_armed.t,
            )
        )
    if arming_known and not segments:
        quality.append(
            ParseIssue("never_armed", "The aircraft never armed in this log, so there is no flight")
        )
