"""Engineer review of findings, and turning reviews into validation labels.

An engineer marks a finding as:

- ``confirmed``: the problem is real;
- ``false_alarm``: FlightLens was wrong;
- ``accepted``: real, but acceptable for this aircraft or mission.

The automatic verdict is never changed by a review; the review is shown next to
it, in the web app and in the report. A review is keyed by the finding's check
and its start second, so it survives re-analysis as long as the same finding
comes back. It stores the finding's check, severity and time window too, so it
can be exported as a ``flightlens validate`` manifest: confirmed and accepted
findings become expected findings, false alarms become ``must_not`` rules.
That turns every review into a regression test for the next FlightLens version.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

STATUSES = ("confirmed", "false_alarm", "accepted")
STATUS_LABEL = {"confirmed": "Confirmed", "false_alarm": "False alarm", "accepted": "Accepted"}
REVIEWABLE = ("problem", "attention")
NOTE_MAX = 2000
TOLERANCE_S = 5.0


def finding_key(finding: Mapping[str, Any]) -> str:
    """Stable id of a finding within its flight: check and start second."""
    start = finding.get("start")
    return f"{finding['check_id']}@{int(start) if start is not None else '-'}"


def with_keys(
    findings: Iterable[Mapping[str, Any]], reviews: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Findings with their ``key`` and ``review`` (or None) attached."""
    out = []
    for f in findings:
        key = finding_key(f)
        r = reviews.get(key)
        out.append(
            {
                **f,
                "key": key,
                "review": {k: r[k] for k in ("status", "note", "reviewed_at")} if r else None,
            }
        )
    return out


def orphaned(
    findings: Iterable[Mapping[str, Any]], reviews: Mapping[str, Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Reviews whose finding is no longer reported (e.g. after re-analysis)."""
    present = {finding_key(f) for f in findings}
    return [r for k, r in reviews.items() if k not in present]


def _rule(r: Mapping[str, Any]) -> dict[str, Any] | str:
    if r.get("t_start") is None:
        return f"{r['check_id']}:{r['severity']}"
    start = float(r["t_start"])
    end = float(r["t_end"]) if r.get("t_end") is not None else start
    return {
        "check": r["check_id"],
        "severity": r["severity"],
        "window": [round(start, 1), round(end, 1)],
        "tolerance_s": TOLERANCE_S,
    }


def manifest(rows: Iterable[Mapping[str, Any]], flights_dir: Path) -> dict[str, Any]:
    """A validation manifest from reviews (rows with flight id, filename, original name).

    ``rows`` carry the review columns plus ``filename`` (as uploaded) and
    ``original`` (the stored file's name, e.g. ``original.bin``).
    """
    logs: dict[str, Any] = {}
    for r in rows:
        entry = logs.setdefault(
            f"{r['flight_id']}/{r['original']}",
            {"source": f"{r['filename']} (engineer review)", "expect": [], "must_not": []},
        )
        if r["status"] == "false_alarm":
            entry["must_not"].append(_rule(r))
        else:
            entry["expect"].append(_rule(r))
    for entry in logs.values():
        for k in ("expect", "must_not"):
            if not entry[k]:
                del entry[k]
    return {"logs_dir": str(flights_dir), "logs": logs}


def manifest_yaml(rows: Iterable[Mapping[str, Any]], flights_dir: Path) -> str:
    header = (
        "# Validation labels exported from FlightLens engineer reviews.\n"
        "# confirmed / accepted -> expect; false alarm -> must_not.\n"
        "# Run: flightlens validate <this file>\n"
    )
    return header + yaml.safe_dump(manifest(rows, flights_dir), sort_keys=False, allow_unicode=True)
