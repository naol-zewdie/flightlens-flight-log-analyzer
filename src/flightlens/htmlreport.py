"""A self-contained HTML report of one flight, printable to PDF.

The report has no external resources (inline CSS, inline SVG charts, no fonts or
scripts to fetch), so it can be archived, emailed or opened offline years
later. Printing it from a browser ("Save as PDF") gives an A4 document; the
print layout keeps each finding on one page where it fits.

The same renderer serves the command line (``flightlens analyze --html``, from
the in-memory flight) and the web app (from the stored report and signals).
Everything that comes from the log (file names, messages) is escaped.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from flightlens.lttb import lttb
from flightlens.reviews import STATUS_LABEL, finding_key

SignalReader = Callable[[str], tuple[np.ndarray, np.ndarray, str] | None]
"""Signal name -> (t, values, unit), or None when the flight does not have it."""

TEMPLATES = Path(__file__).parent / "templates"
ISSUE = ("problem", "attention", "error")
CHART_W, CHART_H = 640, 150
PAD_L, PAD_R, PAD_T, PAD_B = 44, 8, 8, 20
COLORS = ("#2a78d6", "#d9822b", "#1e9e6a")
MAX_SERIES = 3
VERDICT_LABEL = {
    "healthy": "Healthy",
    "attention": "Attention",
    "problem": "Problem",
    "insufficient data": "Insufficient data",
}


def fmt_num(x: float | None) -> str:
    """Engineering numbers, as in the web app: 15,869 · 43.4 · 3.52 · 0.84."""
    if x is None or not math.isfinite(x):
        return "—"
    a = abs(x)
    if a >= 1000:
        return f"{round(x):,}"
    if a >= 100:
        return f"{x:.0f}"
    if a >= 10 or float(x).is_integer():
        return f"{x:.1f}".rstrip("0").rstrip(".")
    return f"{x:.2f}"


def fmt_time(s: float | None) -> str:
    if s is None or not math.isfinite(s):
        return "—"
    total = round(abs(s))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    sign = "−" if s < 0 and total else ""
    return f"{sign}{h}:{m:02d}:{sec:02d}" if h else f"{sign}{m:02d}:{sec:02d}"


def fmt_window(start: float | None, end: float | None) -> str:
    if start is None:
        return ""
    if end is None or end - start < 1:
        return fmt_time(start)
    return f"{fmt_time(start)}–{fmt_time(end)}"


def fmt_duration(s: float | None) -> str:
    if s is None or not math.isfinite(s):
        return "—"
    if s < 60:
        return f"{round(s)} s"
    m, sec = divmod(round(s), 60)
    return f"{m} min {sec} s" if m < 60 else f"{m // 60} h {m % 60} min"


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) / step) + 1)]


def evidence_chart(
    finding: Mapping[str, Any], read: SignalReader, duration: float | None
) -> Markup | None:
    """SVG of the finding's evidence signals around its time window."""
    series = []
    unit = None
    for name in finding.get("evidence_signals") or []:
        got = read(name)
        if got is None:
            continue
        t, v, u = got
        if unit is None:
            unit = u
        if u != unit:  # one y axis per chart
            continue
        series.append((name, t, v))
        if len(series) == MAX_SERIES:
            break
    if not series:
        return None

    start, end = finding.get("start"), finding.get("end")
    t_all = np.concatenate([s[1] for s in series])
    lo_t, hi_t = float(np.nanmin(t_all)), float(np.nanmax(t_all))
    if duration:
        hi_t = max(hi_t, 0.0)
    if start is not None:
        e = end if end is not None else start
        pad = max(10.0, 0.5 * (e - start))
        lo_t, hi_t = max(lo_t, start - pad), min(hi_t, e + pad)
    if hi_t <= lo_t:
        hi_t = lo_t + 1.0

    clipped = []
    for name, t, v in series:
        sel = (t >= lo_t) & (t <= hi_t) & np.isfinite(v)
        if sel.sum() >= 2:
            tt, vv = lttb(t[sel], v[sel], 500)
            clipped.append((name, np.asarray(tt, dtype=float), np.asarray(vv, dtype=float)))
    if not clipped:
        return None

    raw_threshold = finding.get("threshold")
    threshold = float(raw_threshold) if raw_threshold is not None else math.nan
    # draw the limit only when it is in the plotted signal's own unit
    show_threshold = finding.get("unit") == unit and math.isfinite(threshold)
    vals = np.concatenate([s[2] for s in clipped])
    lo_v, hi_v = float(np.min(vals)), float(np.max(vals))
    if show_threshold:
        lo_v, hi_v = min(lo_v, threshold), max(hi_v, threshold)
    if hi_v - lo_v < 1e-9:
        lo_v, hi_v = lo_v - 1, hi_v + 1
    margin = 0.08 * (hi_v - lo_v)
    lo_v, hi_v = lo_v - margin, hi_v + margin

    pw, ph = CHART_W - PAD_L - PAD_R, CHART_H - PAD_T - PAD_B

    def x(tv: float) -> float:
        return PAD_L + (tv - lo_t) / (hi_t - lo_t) * pw

    def y(vv: float) -> float:
        return PAD_T + (1 - (vv - lo_v) / (hi_v - lo_v)) * ph

    parts = [
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" class="chart" role="img" '
        f'aria-label="{escape(", ".join(s[0] for s in clipped))} around the finding">'
    ]
    if start is not None:
        e = end if end is not None else start
        x0, x1 = x(max(start, lo_t)), x(min(e, hi_t))
        parts.append(
            f'<rect x="{x0:.1f}" y="{PAD_T}" width="{max(x1 - x0, 1.5):.1f}" height="{ph}" '
            'class="win"/>'
        )
    for tv in _ticks(lo_v, hi_v, 4):
        parts.append(
            f'<line x1="{PAD_L}" x2="{CHART_W - PAD_R}" y1="{y(tv):.1f}" y2="{y(tv):.1f}" '
            f'class="grid"/><text x="{PAD_L - 6}" y="{y(tv) + 3:.1f}" class="yl">'
            f"{escape(fmt_num(tv))}</text>"
        )
    for tv in _ticks(lo_t, hi_t, 6):
        parts.append(
            f'<text x="{x(tv):.1f}" y="{CHART_H - 5}" class="xl">{escape(fmt_time(tv))}</text>'
        )
    if show_threshold:
        parts.append(
            f'<line x1="{PAD_L}" x2="{CHART_W - PAD_R}" y1="{y(threshold):.1f}" '
            f'y2="{y(threshold):.1f}" class="thr"/>'
        )
    for i, (_name, tt, vv) in enumerate(clipped):
        pts = " ".join(f"{x(a):.1f},{y(b):.1f}" for a, b in zip(tt, vv, strict=True))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{COLORS[i]}" class="ln"/>')
    parts.append("</svg>")
    legend = "".join(
        f'<span><i style="background:{COLORS[i]}"></i>{escape(name)}</span>'
        for i, (name, _, _) in enumerate(clipped)
    )
    if show_threshold:
        legend += '<span><i class="thr-key"></i>limit</span>'
    legend += f'<span class="unit">{escape(unit or "")}</span>'
    return Markup(f'{"".join(parts)}<div class="legend">{legend}</div>')


def render_html(
    report: Mapping[str, Any],
    read_signal: SignalReader,
    *,
    filename: str | None = None,
    aircraft: str | None = None,
    reviews: Mapping[str, Mapping[str, Any]] | None = None,
    stale: Sequence[str] = (),
    generated: datetime | None = None,
) -> str:
    """The report as one HTML document."""
    reviews = reviews or {}
    source = report.get("source") or {}
    duration = source.get("log_duration_s")
    findings = []
    for f in report.get("findings") or []:
        if f.get("severity") not in ISSUE:
            continue
        review = reviews.get(finding_key(f))
        findings.append(
            {
                **f,
                "window": fmt_window(f.get("start"), f.get("end")),
                "chart": evidence_chart(f, read_signal, duration),
                "review": {**review, "label": STATUS_LABEL.get(review["status"], review["status"])}
                if review
                else None,
            }
        )
    not_evaluated = [
        f for f in report.get("findings") or [] if f.get("severity") == "not_evaluated"
    ]
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(num=fmt_num, time=fmt_time, duration=fmt_duration)
    path = source.get("path")
    return env.get_template("report.html.j2").render(
        r=report,
        source=source,
        summary=report.get("summary") or {},
        filename=filename or (Path(path).name if path else "flight log"),
        aircraft=aircraft,
        verdict_label=VERDICT_LABEL.get(report.get("verdict", ""), report.get("verdict")),
        findings=findings,
        passed=[c for c in report.get("checks") or [] if c.get("status") == "ok"],
        not_evaluated=not_evaluated,
        n_reviewed=sum(1 for f in findings if f["review"]),
        stale=list(stale),
        generated=(generated or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC"),
    )


def flight_reader(signals: Mapping[str, Any]) -> SignalReader:
    """Reader over an in-memory flight's signals (``Flight.signals``)."""

    def read(name: str) -> tuple[np.ndarray, np.ndarray, str] | None:
        s = signals.get(name)
        return (s.t, s.values, s.unit) if s is not None and len(s) > 1 else None

    return read
