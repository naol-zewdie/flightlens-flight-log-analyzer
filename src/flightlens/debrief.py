"""The printable debrief report: the written record of a flight's review.

Next to the automatic health report (``htmlreport.py``), it gathers what the
engineers concluded: the debrief (status, summary, conclusion, actions), one
chronological timeline merging flight modes, events, findings with their
review status and the notes written during replay, a top-down track with the
notes and findings placed on it, and provenance (FlightLens version, log and
video SHA-256, how each video was synced). One self-contained HTML page, in
the same style as the health report; print it to PDF from the browser.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import numpy as np
from jinja2 import Environment, FileSystemLoader, select_autoescape

from flightlens import __version__
from flightlens.htmlreport import (
    TEMPLATES,
    VERDICT_LABEL,
    fmt_duration,
    fmt_num,
    fmt_time,
    fmt_window,
)
from flightlens.reviews import STATUS_LABEL, finding_key

ISSUE = ("problem", "attention")
EVENT_KINDS = {"mode", "arm", "disarm", "failsafe", "error"}
SYNC_LABEL = {
    "points": "sync points",
    "manual": "set by hand",
    "metadata": "camera clock (a guess)",
    "none": "not synced",
}
TRACK_POINTS = 1500


def timeline(
    report: Mapping[str, Any],
    notes: Sequence[Mapping[str, Any]],
    reviews: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Modes, notable events, timed findings and notes, in time order."""
    items: list[dict[str, Any]] = []
    for e in report.get("events") or []:
        notable = e.get("kind") in EVENT_KINDS or e.get("level") in ("warning", "error", "critical")
        if not notable:
            continue
        items.append(
            {
                "t": e["t"],
                "kind": "mode" if e.get("kind") == "mode" else "event",
                "label": e.get("text", ""),
                "level": e.get("level", "info"),
            }
        )
    for f in report.get("findings") or []:
        if f.get("severity") in ISSUE and f.get("start") is not None:
            review = reviews.get(finding_key(f))
            items.append(
                {
                    "t": f["start"],
                    "end": f.get("end"),
                    "kind": "finding",
                    "label": f["title"],
                    "severity": f["severity"],
                    "review": STATUS_LABEL.get(review["status"], review["status"])
                    if review
                    else None,
                    "review_note": review.get("note") if review else None,
                }
            )
    for i, n in enumerate(notes, start=1):
        items.append(
            {
                "t": n["t_start"],
                "end": n.get("t_end"),
                "kind": "note",
                "number": i,
                "label": n["text"],
                "tags": n.get("tags") or [],
                "author": n.get("author"),
            }
        )
    order = {"mode": 0, "event": 1, "finding": 2, "note": 3}
    return sorted(items, key=lambda x: (x["t"], order[x["kind"]]))


def track_svg(
    cols: Mapping[str, np.ndarray] | None,
    meta: Mapping[str, Any] | None,
    notes: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
) -> str | None:
    """Top-down path in metres from take-off, with numbered notes and finding stretches."""
    if not cols or not meta or "east" not in cols or meta.get("origin") is None:
        return None
    t = cols["t"]
    e, n = cols["east"].astype(float), cols["north"].astype(float)
    ok = (cols["valid_pos"] > 0) & np.isfinite(e) & np.isfinite(n)
    if ok.sum() < 2:
        return None
    w, h, pad = 640.0, 400.0, 24.0
    lo_e, hi_e, lo_n, hi_n = e[ok].min(), e[ok].max(), n[ok].min(), n[ok].max()
    # one scale for both axes; at least 20 m across, so a hover is not blown up
    span = max(hi_e - lo_e, hi_n - lo_n, 20.0)
    scale = min((w - 2 * pad) / max(hi_e - lo_e, 20.0), (h - 2 * pad - 20) / max(hi_n - lo_n, 20.0))
    cx, cn = (lo_e + hi_e) / 2, (lo_n + hi_n) / 2

    def xy(east: float, north: float) -> tuple[float, float]:
        return w / 2 + (east - cx) * scale, h / 2 - (north - cn) * scale

    def path(mask: np.ndarray) -> str:
        """Polyline segments over ``mask``, broken at gaps, decimated."""
        idx = np.flatnonzero(mask)
        if idx.size < 2:
            return ""
        step = max(1, idx.size // TRACK_POINTS)
        out, run = [], []
        last = None
        for k in idx[::step]:
            if last is not None and k - last > step * 2:
                if len(run) > 1:
                    out.append("M" + " L".join(run))
                run = []
            px, py = xy(e[k], n[k])
            run.append(f"{px:.1f},{py:.1f}")
            last = k
        if len(run) > 1:
            out.append("M" + " L".join(run))
        return " ".join(out)

    parts = [
        f'<svg class="track" viewBox="0 0 {w:.0f} {h:.0f}" role="img" '
        'aria-label="Flight path from above, north up, with notes and findings">',
        f'<rect width="{w:.0f}" height="{h:.0f}" class="ground"/>',
        f'<path d="{path(ok)}" class="trk"/>',
    ]
    for f in findings:
        if f.get("severity") not in ISSUE or f.get("start") is None:
            continue
        sel = ok & (t >= f["start"]) & (t <= (f.get("end") or f["start"]) + 0.5)
        d = path(sel)
        if d:
            parts.append(
                f'<path d="{d}" class="win {f["severity"]}">'
                f"<title>{html.escape(f['title'])}</title></path>"
            )
    ox, oy = xy(0.0, 0.0)
    parts.append(
        f'<circle cx="{ox:.1f}" cy="{oy:.1f}" r="6" class="home"><title>take-off</title></circle>'
    )
    for i, note in enumerate(notes, start=1):
        k = int(np.clip(np.searchsorted(t, note["t_start"]), 0, t.size - 1))
        if not ok[k]:
            continue
        px, py = xy(e[k], n[k])
        parts.append(
            f'<g class="note"><circle cx="{px:.1f}" cy="{py:.1f}" r="9"/>'
            f'<text x="{px:.1f}" y="{py + 3.5:.1f}">{i}</text>'
            f"<title>{html.escape(note['text'][:120])}</title></g>"
        )
    # a scale bar labelled in metres, and north
    step = _nice(span / 5)
    bar = step * scale
    parts.append(
        f'<line x1="{pad:.1f}" x2="{pad + bar:.1f}" y1="{h - 14:.0f}" '
        f'y2="{h - 14:.0f}" class="bar"/>'
    )
    parts.append(
        f'<text x="{pad + bar + 6:.1f}" y="{h - 10:.0f}" class="lbl">{fmt_num(step)} m</text>'
    )
    parts.append(f'<text x="{w - pad:.0f}" y="{pad:.0f}" class="lbl" text-anchor="end">N ↑</text>')
    parts.append("</svg>")
    return "".join(parts)


def _nice(raw: float) -> float:
    p = 10 ** np.floor(np.log10(max(raw, 1e-6)))
    f = raw / p
    return float((1 if f < 1.5 else 2 if f < 3.5 else 5 if f < 7.5 else 10) * p)


def render_debrief(
    report: Mapping[str, Any],
    *,
    filename: str,
    aircraft: str | None,
    debrief: Mapping[str, Any] | None,
    notes: Sequence[Mapping[str, Any]],
    reviews: Mapping[str, Mapping[str, Any]],
    videos: Sequence[Mapping[str, Any]] = (),
    replay_cols: Mapping[str, np.ndarray] | None = None,
    replay_meta: Mapping[str, Any] | None = None,
    generated: datetime | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(num=fmt_num, time=fmt_time, duration=fmt_duration)
    env.globals["window"] = fmt_window
    findings = list(report.get("findings") or [])
    standing = [
        {
            **f,
            "review": STATUS_LABEL.get(reviews[finding_key(f)]["status"])
            if finding_key(f) in reviews
            else None,
        }
        for f in findings
        if f.get("severity") in ISSUE and f.get("start") is None
    ]
    return env.get_template("debrief.html.j2").render(
        r=report,
        source=report.get("source") or {},
        summary=report.get("summary") or {},
        filename=filename,
        aircraft=aircraft,
        verdict_label=VERDICT_LABEL.get(report.get("verdict", ""), report.get("verdict")),
        debrief=debrief,
        items=timeline(report, notes, reviews),
        standing=standing,
        n_notes=len(notes),
        track=track_svg(replay_cols, replay_meta, notes, findings),
        videos=[
            {**v, "sync_label": SYNC_LABEL.get(v.get("sync_method", "none"), "")} for v in videos
        ],
        version=__version__,
        saved_at=datetime.fromtimestamp(debrief["updated_at"], UTC).strftime("%Y-%m-%d %H:%M UTC")
        if debrief
        else None,
        generated=(generated or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC"),
    )
