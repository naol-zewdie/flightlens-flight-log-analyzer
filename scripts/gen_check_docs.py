"""Write docs/checks.md from the check registry: one section per check module.

    uv run python scripts/gen_check_docs.py

Each section is the module's docstring (what the check looks at and when it
reports Attention or Problem), then per check its id, version, signals and
default thresholds, exactly as the code has them.
"""

from __future__ import annotations

import inspect
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from flightlens import __version__
from flightlens.checks import all_checks
from flightlens.fleet import MIN_HISTORY
from flightlens.metrics import METRICS, MIN_AIR_S

OUT = Path(__file__).resolve().parent.parent / "docs" / "checks.md"


def main() -> int:
    by_module: dict[str, list[Any]] = defaultdict(list)
    for c in all_checks():
        by_module[type(c).__module__].append(c)
    lines = [
        "# Check reference",
        "",
        f"Generated from FlightLens {__version__} by `scripts/gen_check_docs.py`; "
        "`flightlens checks` prints the same from the installed version. Thresholds are "
        "starting values, not limits approved for your aircraft; override them with "
        '`analyze(flight, config={"vibration.level": {"attention": 25}})`.',
        "",
    ]
    for module, checks in sorted(by_module.items(), key=lambda kv: kv[1][0].id):
        doc = inspect.cleandoc(sys.modules[module].__doc__ or "")
        title, _, body = doc.partition("\n")
        lines += [f"## {title.rstrip('.')}", "", body.strip(), ""]
        for c in checks:
            lines += [
                f"### `{c.id}` · {c.title}",
                "",
                f"Version {c.version} · signals: "
                + (", ".join(f"`{s}`" for s in c.required_signals) or "none (events)")
                + "",
                "",
                "| Setting | Default |",
                "|---|---|",
                *[f"| `{k}` | {v:g} |" for k, v in c.defaults.items()],
                "",
            ]
    lines += [
        "## Trend metrics (aircraft pages)",
        "",
        "Computed for every flight and compared with the same aircraft's earlier flights "
        f"(at least {MIN_HISTORY}). A flight is flagged when it is worse than their median by "
        "more than both minimums below and by more than 3 robust standard deviations of "
        f"those flights. Signal metrics need {MIN_AIR_S:.0f} s in the air.",
        "",
        "| Metric | Unit | Worse when | Drift: at least | What it shows |",
        "|---|---|---|---|---|",
    ]
    for m in METRICS:
        worse = {1: "higher", -1: "lower", 0: "(context only)"}[m.worse]
        parts = ([f"{m.rel:.0%}"] if m.rel else []) + [f"{m.abs_min:g} {m.unit or 'points'}"]
        drift = "–" if m.worse == 0 else " and ".join(parts)
        lines.append(f"| {m.label} | {m.unit or '–'} | {worse} | {drift} | {m.meaning} |")
    lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"{sum(len(v) for v in by_module.values())} checks -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
