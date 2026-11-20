"""Score FlightLens against logs whose outcome is known.

A *manifest* (YAML) names a folder of logs and, per log, what FlightLens must
and must not report::

    logs_dir: logs                 # relative to the manifest file
    defaults:
      must_not: [vibration.level:problem]
      exhaustive: false
    logs:
      vibe-z.bin:
        source: ArduPilot SITL Copter 4.6     # free text, for the report
        fault: vibration                      # groups the benchmark table
        verdict: problem
        expect:
          - vibration.level:problem           # short form
          - {check: imu.clipping, severity: attention, window: [50, 62], tolerance_s: 5}
        allow: [battery.sag]                  # may fire, never counted
        must_not: [gps.quality]
        exhaustive: true                      # anything else is a false alarm
        crash_related: [Aircraft out of trim (roll)]

``check:severity`` means that check with exactly that severity; a bare
``check`` means any Attention or Problem from it. ``must`` is accepted as an
alias of ``expect``.

Scoring, per check:

- an expected finding is *detected* when the check reports Attention or Problem
  (inside the window ± tolerance when a window is given); it is *exact* when the
  severity also matches;
- a *false alarm* is an Attention/Problem that matches ``must_not``, or, on an
  exhaustive log, one that is neither expected nor allowed.

Recall = detected / expected; precision = detected / (detected + false alarms).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from flightlens import parsers
from flightlens.engine import Report, analyze
from flightlens.model import Finding

ISSUE = ("attention", "problem")


@dataclass(frozen=True)
class Rule:
    check: str
    severity: str | None = None
    window: tuple[float, float] | None = None
    tolerance_s: float = 5.0

    @classmethod
    def parse(cls, raw: Any) -> Rule:
        if isinstance(raw, str):
            check, _, sev = raw.partition(":")
            return cls(check.strip(), sev.strip() or None)
        if isinstance(raw, dict) and "check" in raw:
            window = raw.get("window")
            return cls(
                str(raw["check"]),
                raw.get("severity"),
                (float(window[0]), float(window[1])) if window else None,
                float(raw.get("tolerance_s", 5.0)),
            )
        raise ValueError(f"not a rule: {raw!r}")

    def __str__(self) -> str:
        return f"{self.check}:{self.severity}" if self.severity else self.check

    def matches(self, f: Finding, *, any_issue_severity: bool = False) -> bool:
        if f.check_id != self.check:
            return False
        sev = f.severity.value
        if self.severity and not any_issue_severity:
            if sev != self.severity:
                return False
        elif sev not in ISSUE:
            return False
        if self.window is None:
            return True
        if f.start is None:
            return False
        lo, hi = self.window[0] - self.tolerance_s, self.window[1] + self.tolerance_s
        return f.start <= hi and (f.end if f.end is not None else f.start) >= lo


@dataclass
class Case:
    path: Path
    name: str
    group: str
    fault: str
    source: str = ""
    verdict: str | None = None
    expect: list[Rule] = field(default_factory=list)
    allow: list[Rule] = field(default_factory=list)
    must_not: list[Rule] = field(default_factory=list)
    exhaustive: bool = False
    crash_related: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.group}/{self.name}"


def load_manifest(path: Path) -> list[Case]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    logs_dir = Path(spec.get("logs_dir", "."))
    if not logs_dir.is_absolute():
        logs_dir = path.parent / logs_dir
    defaults = spec.get("defaults") or {}
    cases = []
    for name, exp in (spec.get("logs") or {}).items():
        exp = exp or {}
        expect = exp.get("expect", exp.get("must", []))
        cases.append(
            Case(
                path=logs_dir / name,
                name=str(name),
                group=path.stem,
                fault=str(exp.get("fault", "healthy" if exp.get("verdict") == "healthy" else "-")),
                source=str(exp.get("source", spec.get("source", ""))),
                verdict=exp.get("verdict"),
                expect=[Rule.parse(r) for r in expect],
                allow=[Rule.parse(r) for r in exp.get("allow", [])],
                must_not=[
                    Rule.parse(r) for r in [*defaults.get("must_not", []), *exp.get("must_not", [])]
                ],
                exhaustive=bool(exp.get("exhaustive", defaults.get("exhaustive", False))),
                crash_related=list(exp.get("crash_related", [])),
            )
        )
    return cases


def find_manifests(paths: Iterable[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        out += sorted(p.glob("*.yaml")) + sorted(p.glob("*.yml")) if p.is_dir() else [p]
    return out


@dataclass
class Outcome:
    case: Case
    report: Report | None = None
    error: str | None = None
    seconds: float = 0.0
    detected: list[tuple[Rule, Finding]] = field(default_factory=list)
    wrong_severity: list[tuple[Rule, Finding]] = field(default_factory=list)
    missed: list[Rule] = field(default_factory=list)
    false_alarms: list[Finding] = field(default_factory=list)
    verdict_ok: bool = True
    missing_related: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and not self.missed
            and not self.wrong_severity
            and not self.false_alarms
            and self.verdict_ok
            and not self.missing_related
        )

    def problems(self) -> list[str]:
        out = []
        if self.error:
            out.append(f"error: {self.error}")
        if not self.verdict_ok and self.report is not None:
            out.append(f"verdict {self.report.verdict.value}, expected {self.case.verdict}")
        out += [f"missed {r}" for r in self.missed]
        out += [f"{r} reported as {f.severity.value}" for r, f in self.wrong_severity]
        out += [
            f"false alarm {f.check_id}:{f.severity.value} ({f.title})" for f in self.false_alarms
        ]
        out += [f"crash cause not linked: {t}" for t in self.missing_related]
        return out


def evaluate(case: Case, config: Mapping[str, Mapping[str, float]] | None = None) -> Outcome:
    """Analyze one labelled log and score it; ``config`` overrides thresholds."""
    out = Outcome(case)
    if not case.path.exists():
        out.error = "log file not found"
        return out
    start = time.perf_counter()
    try:
        report = analyze(parsers.load(case.path), config=config)
    except Exception as exc:  # a parser or check crash is a benchmark failure, not an abort
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    out.seconds = time.perf_counter() - start
    out.report = report
    findings = report.findings

    for rule in case.expect:
        exact = next((f for f in findings if rule.matches(f)), None)
        if exact is not None:
            out.detected.append((rule, exact))
            continue
        loose = next((f for f in findings if rule.matches(f, any_issue_severity=True)), None)
        if loose is not None:
            out.wrong_severity.append((rule, loose))
        else:
            out.missed.append(rule)

    claimed = {id(f) for _, f in out.detected + out.wrong_severity}
    for f in findings:
        if f.severity.value not in ISSUE or id(f) in claimed:
            continue
        forbidden = any(r.matches(f) for r in case.must_not)
        expected = any(r.matches(f, any_issue_severity=True) for r in case.expect)
        allowed = any(r.matches(f, any_issue_severity=True) for r in case.allow)
        if forbidden or (case.exhaustive and not expected and not allowed):
            out.false_alarms.append(f)

    if case.verdict is not None:
        out.verdict_ok = report.verdict.value == case.verdict
    if case.crash_related:
        crash = next((f for f in findings if f.check_id == "flight.crash"), None)
        related = crash.related if crash else ()
        out.missing_related = [t for t in case.crash_related if t not in related]
    return out


# --- summary -------------------------------------------------------------------


@dataclass
class CheckStats:
    expected: int = 0
    detected: int = 0
    exact: int = 0
    false_alarms: int = 0
    timing: list[float] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return self.detected / self.expected if self.expected else None

    @property
    def precision(self) -> float | None:
        n = self.detected + self.false_alarms
        return self.detected / n if n else None

    @property
    def timing_s(self) -> float | None:
        return statistics.median(self.timing) if self.timing else None


def per_check(outcomes: list[Outcome]) -> dict[str, CheckStats]:
    stats: dict[str, CheckStats] = {}

    def get(check: str) -> CheckStats:
        return stats.setdefault(check, CheckStats())

    for o in outcomes:
        for rule in o.case.expect:
            get(rule.check).expected += 1
        for rule, f in o.detected + o.wrong_severity:
            s = get(rule.check)
            s.detected += 1
            if (rule, f) in o.detected:
                s.exact += 1
            if rule.window is not None and f.start is not None:
                s.timing.append(abs(f.start - rule.window[0]))
        for f in o.false_alarms:
            get(f.check_id).false_alarms += 1
    return dict(sorted(stats.items()))


def per_fault(outcomes: list[Outcome]) -> dict[str, tuple[int, int]]:
    """fault -> (logs fully correct, logs)."""
    out: dict[str, tuple[int, int]] = {}
    for o in outcomes:
        ok, n = out.get(o.case.fault, (0, 0))
        out[o.case.fault] = (ok + o.passed, n + 1)
    return dict(sorted(out.items()))


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def to_dict(outcomes: list[Outcome]) -> dict[str, Any]:
    return {
        "logs": len(outcomes),
        "passed": sum(o.passed for o in outcomes),
        "checks": {
            k: {
                "expected": s.expected,
                "detected": s.detected,
                "exact_severity": s.exact,
                "false_alarms": s.false_alarms,
                "recall": s.recall,
                "precision": s.precision,
                "median_timing_error_s": s.timing_s,
            }
            for k, s in per_check(outcomes).items()
        },
        "cases": [
            {
                "id": o.case.id,
                "fault": o.case.fault,
                "passed": o.passed,
                "verdict": o.report.verdict.value if o.report else None,
                "problems": o.problems(),
                "seconds": round(o.seconds, 3),
            }
            for o in outcomes
        ],
    }


def to_markdown(outcomes: list[Outcome], title: str = "Validation results") -> str:
    lines = [f"# {title}", ""]
    passed = sum(o.passed for o in outcomes)
    lines += [f"**{passed} of {len(outcomes)} logs fully correct.**", ""]
    lines += ["## By fault", "", "| Fault | Logs fully correct |", "|---|---|"]
    for fault, (ok, n) in per_fault(outcomes).items():
        lines.append(f"| {fault} | {ok} / {n} |")
    lines += [
        "",
        "## By check",
        "",
        "| Check | Expected | Detected | Right severity | False alarms | Recall | Precision "
        "| Median start error |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for check, s in per_check(outcomes).items():
        timing = "–" if s.timing_s is None else f"{s.timing_s:.1f} s"
        lines.append(
            f"| `{check}` | {s.expected} | {s.detected} | {s.exact} | {s.false_alarms} "
            f"| {_pct(s.recall)} | {_pct(s.precision)} | {timing} |"
        )
    failing = [o for o in outcomes if not o.passed]
    if failing:
        lines += ["", "## Not fully correct", ""]
        for o in failing:
            lines.append(f"- `{o.case.id}`: " + "; ".join(o.problems()))
    return "\n".join(lines) + "\n"
