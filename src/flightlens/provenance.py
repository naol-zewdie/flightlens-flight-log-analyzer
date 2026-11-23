"""Which versions produced an analysis, and whether it is out of date.

Every report records the version of each check and of the signal mapping it
used. When FlightLens is upgraded, a flight whose report was made by a check
or mapping that has since changed is *stale*: its verdict may differ from what
the current version would say, so the library offers to re-analyze it.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

MAPPING_FOR_FORMAT = {"dataflash": "ardupilot", "ulog": "px4", "tlog": "mavlink"}


@lru_cache(maxsize=1)
def current_versions() -> dict[str, Any]:
    """Versions the running FlightLens would analyze with."""
    from flightlens.checks import all_checks
    from flightlens.parsers._mapping import load_mapping

    return {
        "checks": {c.id: c.version for c in all_checks()},
        "mappings": {
            fmt: str(load_mapping(name)["version"]) for fmt, name in MAPPING_FOR_FORMAT.items()
        },
    }


def versions_of(report: Mapping[str, Any]) -> dict[str, Any]:
    """The versions a stored report was made with (``Report.to_dict()`` layout)."""
    source = report.get("source") or {}
    return {
        "checks": {c["id"]: c["version"] for c in report.get("checks") or []},
        "mapping": source.get("mapping_version"),
        "format": source.get("format"),
    }


def stale_reasons(versions: Mapping[str, Any] | None, has_metrics: bool = True) -> list[str]:
    """Why an analysis made with ``versions`` is out of date; empty when it is current."""
    reasons: list[str] = []
    if not has_metrics:
        reasons.append("analyzed before trend metrics existed")
    if not versions:
        return reasons or ["the versions it was analyzed with are unknown"]
    now = current_versions()
    then = versions.get("checks") or {}
    for check_id, version in now["checks"].items():
        old = then.get(check_id)
        if old is None:
            reasons.append(f"new check {check_id}")
        elif old != version:
            reasons.append(f"{check_id} {old} → {version}")
    current_mapping = now["mappings"].get(versions.get("format") or "")
    if current_mapping and versions.get("mapping") and versions["mapping"] != current_mapping:
        reasons.append(f"signal mapping {versions['mapping']} → {current_mapping}")
    return reasons
