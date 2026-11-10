"""Command-line interface.

Exit codes for ``analyze``:
  0  healthy        1  attention        2  problem
  3  the log could not be analyzed (unsupported, corrupt, parser missing)
  64 usage error (bad arguments)
"""

from __future__ import annotations

import csv
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from flightlens import __version__, parsers
from flightlens.checks import all_checks
from flightlens.engine import Report, Verdict, analyze
from flightlens.intervals import format_time
from flightlens.model import Finding, Severity
from flightlens.summary import summarize

EXIT_CANNOT_ANALYZE = 3
EXIT_USAGE = 64
LOG_SUFFIXES = {".ulg", ".bin", ".tlog"}

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Automatic drone flight log analysis.",
)
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

_SEVERITY_STYLE: dict[Severity, tuple[str, str, str, str]] = {
    # icon, plain-text icon, label, style; colour is never the only cue
    Severity.PROBLEM: ("✖", "x", "Problem", "bold red"),
    Severity.ATTENTION: ("▲", "!", "Attention", "yellow"),
    Severity.ERROR: ("!", "E", "Error", "magenta"),
    Severity.NOT_EVALUATED: ("○", "-", "Not evaluated", "dim"),
    Severity.OK: ("✔", "+", "OK", "green"),
}
_VERDICT_STYLE = {
    Verdict.HEALTHY: "bold green",
    Verdict.ATTENTION: "bold yellow",
    Verdict.PROBLEM: "bold red",
    Verdict.INSUFFICIENT_DATA: "bold",
}


def _supports_icons(out: Console) -> bool:
    """Legacy Windows consoles (cp1252) cannot print the icon glyphs."""
    try:
        "✖▲✔○".encode(out.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _load_or_exit(log: Path) -> Any:
    if not log.is_file():
        err_console.print(f"[red]error:[/] {log} does not exist or is not a file")
        raise typer.Exit(EXIT_CANNOT_ANALYZE)
    try:
        return parsers.load(log)
    except (parsers.ParseError, OSError) as exc:
        err_console.print(f"[red]error:[/] cannot analyze {log.name}: {exc}")
        raise typer.Exit(EXIT_CANNOT_ANALYZE) from None


@app.command("analyze")
def analyze_cmd(
    log: Annotated[Path, typer.Argument(help="Path to a .bin, .ulg or .tlog file.")],
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the report as JSON.")
    ] = None,
) -> None:
    """Analyze a flight log and print the verdict and findings."""
    report = analyze(_load_or_exit(log))
    render_report(report, console)

    if json_out is not None:
        try:
            json_out.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]error:[/] cannot write {json_out}: {exc}")
            raise typer.Exit(EXIT_CANNOT_ANALYZE) from None
        console.print(f"\nJSON report written to {json_out}")

    raise typer.Exit(report.verdict.exit_code)


@app.command("signals")
def signals_cmd(
    log: Annotated[Path, typer.Argument(help="Path to a flight log.")],
) -> None:
    """List the signals found in a log, with their source and sample rate."""
    flight = _load_or_exit(log)
    table = Table(box=None, header_style="bold", pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("Signal", "Unit", "Samples", "Rate", "Min", "Max", "Source"):
        table.add_column(
            col, justify="right" if col in {"Samples", "Rate", "Min", "Max"} else "left"
        )
    for name, sig in sorted(flight.signals.items()):
        span = float(sig.t[-1] - sig.t[0]) if len(sig) > 1 else 0.0
        rate = f"{(len(sig) - 1) / span:.0f} Hz" if span > 0 else ""
        finite = sig.values[np.isfinite(sig.values)]
        lo = f"{finite.min():.4g}" if finite.size else "—"
        hi = f"{finite.max():.4g}" if finite.size else "—"
        table.add_row(name, sig.unit, str(len(sig)), rate, lo, hi, Text(sig.source, style="dim"))
    console.print(table)
    _print_quality(flight, console)


@app.command("batch")
def batch_cmd(
    folder: Annotated[Path, typer.Argument(help="Folder with logs (searched recursively).")],
    csv_out: Annotated[
        Path | None, typer.Option("--csv", help="Write one row per log to this CSV file.")
    ] = None,
) -> None:
    """Analyze every log in a folder and summarize how the system behaved."""
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in LOG_SUFFIXES)
    if not files:
        err_console.print(f"[red]error:[/] no .ulg, .bin or .tlog files in {folder}")
        raise typer.Exit(EXIT_CANNOT_ANALYZE)

    rows: list[dict[str, Any]] = []
    not_evaluated: Counter[str] = Counter()
    for path in files:
        row: dict[str, Any] = {"file": path.name, "size_mb": round(path.stat().st_size / 1e6, 1)}
        t0 = time.perf_counter()
        try:
            flight = parsers.load(path)
            report = analyze(flight)
        except parsers.ParseError as exc:
            row.update(status="rejected", detail=str(exc))
        except Exception as exc:  # a crash is a bug to fix, not a verdict
            row.update(status="crash", detail=f"{type(exc).__name__}: {exc}")
        else:
            md, summ = flight.metadata, summarize(flight)
            counts = Counter(f.severity for f in report.findings)
            skipped = [r.check_id for r in report.incomplete]
            not_evaluated.update(skipped)
            row.update(
                status="ok",
                firmware=md.firmware or "",
                vehicle=md.vehicle_type or "",
                duration_s=round(summ.duration_s or 0),
                verdict=report.verdict.value,
                score=report.score,
                problems=counts[Severity.PROBLEM],
                attention=counts[Severity.ATTENTION],
                not_evaluated=" ".join(skipped),
                quality_issues=len(flight.quality),
                detail="; ".join(
                    f.title
                    for f in report.findings
                    if f.severity in (Severity.PROBLEM, Severity.ATTENTION)
                ),
            )
        row["seconds"] = round(time.perf_counter() - t0, 2)
        rows.append(row)
        _print_batch_row(row)

    _print_batch_summary(rows, not_evaluated, console)
    if csv_out is not None:
        keys = list(dict.fromkeys(k for r in rows for k in r))
        with csv_out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"CSV written to {csv_out}")


def _print_batch_row(row: dict[str, Any]) -> None:
    name = row["file"][:12]
    if row["status"] != "ok":
        style = "red" if row["status"] == "crash" else "dim"
        console.print(Text(f"{name:<12}  {row['status'].upper():<9}  {row['detail']}", style=style))
        return
    verdict = Verdict(row["verdict"])
    line = Text(f"{name:<12}  ")
    line.append(f"{verdict.value.upper():<9}", style=_VERDICT_STYLE[verdict])
    score = row["score"] if row["score"] is not None else "-"
    line.append(
        f"  {score:>3}  {row['firmware'][:22]:<22}  {row['vehicle'][:14]:<14}"
        f"  {format_time(row['duration_s'])}  {row['seconds']:>5.2f}s  "
    )
    line.append(row["detail"][:70], style="")
    console.print(line)


def _print_batch_summary(rows: list[dict[str, Any]], skipped: Counter[str], out: Console) -> None:
    status = Counter(r["status"] for r in rows)
    verdicts = Counter(r.get("verdict") for r in rows if r["status"] == "ok")
    ok = status["ok"]
    out.print()
    out.print(
        f"{len(rows)} logs · {ok} analyzed · {status['rejected']} rejected · "
        f"{status['crash']} crashed"
    )
    if ok:
        mean_s = np.mean([r["seconds"] for r in rows if r["status"] == "ok"])
        out.print(
            "verdicts: "
            + " · ".join(f"{v} {verdicts[v.value]}" for v in Verdict)
            + f"   ·   mean time {mean_s:.2f}s"
        )
    if skipped:
        out.print(
            "not evaluated: " + ", ".join(f"{cid} {n}/{ok}" for cid, n in skipped.most_common())
        )


@app.command("checks")
def checks_cmd() -> None:
    """List the available checks and their default thresholds."""
    table = Table(box=None, header_style="bold", pad_edge=False)
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Version")
    table.add_column("Signals")
    table.add_column("Defaults")
    for c in all_checks():
        defaults = ", ".join(f"{k}={v:g}" for k, v in c.defaults.items())
        table.add_row(c.id, c.title, c.version, ", ".join(c.required_signals), defaults)
    console.print(table)


@app.command("serve")
def serve_cmd(
    host: Annotated[str, typer.Option(help="Interface to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    data: Annotated[
        Path | None, typer.Option(help="Data folder (default: $FLIGHTLENS_DATA or ./data).")
    ] = None,
    web: Annotated[
        Path | None,
        typer.Option(help="Built web UI folder (default: $FLIGHTLENS_WEB or web/dist)."),
    ] = None,
    allow_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allow-host",
            help="Extra host name the server answers to, e.g. a LAN IP (repeatable; "
            "also $FLIGHTLENS_ALLOWED_HOSTS, comma-separated). Default: localhost only.",
        ),
    ] = None,
) -> None:
    """Start the web app and API."""
    import os

    import uvicorn

    from flightlens.api import LOCAL_HOSTS, create_app

    hosts = [*LOCAL_HOSTS, *(allow_host or [])]
    hosts += [h.strip() for h in os.environ.get("FLIGHTLENS_ALLOWED_HOSTS", "").split(",")]
    if host not in ("0.0.0.0", "::"):
        hosts.append(host)
    hosts = [h for h in hosts if h]

    data_dir = data or Path(os.environ.get("FLIGHTLENS_DATA", "data"))
    web_dir = web or Path(os.environ.get("FLIGHTLENS_WEB", "web/dist"))
    if not (web_dir / "index.html").exists():
        err_console.print(f"[yellow]note:[/] no web UI at {web_dir}; serving the API only")
    console.print(f"FlightLens {__version__} · data in {data_dir.resolve()}")
    console.print(f"Open http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}")
    uvicorn.run(
        create_app(data_dir, web_dir, allowed_hosts=hosts),
        host=host,
        port=port,
        log_level="warning",
    )


@app.command("version")
def version_cmd() -> None:
    """Print the FlightLens version."""
    console.print(__version__)


def render_report(report: Report, out: Console) -> None:
    md = report.flight.metadata
    summ = summarize(report.flight)
    header = " · ".join(
        p
        for p in (
            f"FlightLens {report.engine_version}",
            md.firmware,
            md.vehicle_type,
            format_time(summ.duration_s) if summ.duration_s is not None else None,
        )
        if p
    )
    out.print(header)
    facts = []
    if summ.distance_m is not None:
        facts.append(f"{summ.distance_m / 1000:.2f} km")
    if summ.max_altitude_m is not None:
        facts.append(f"max alt {summ.max_altitude_m:.0f} m")
    if summ.max_speed_m_s is not None:
        facts.append(f"max speed {summ.max_speed_m_s:.1f} m/s")
    if summ.battery_used_mah is not None:
        facts.append(f"{summ.battery_used_mah:.0f} mAh used")
    if summ.modes:
        facts.append(", ".join(summ.modes))
    if facts:
        out.print(Text(" · ".join(facts), style="dim"))
    verdict = Text("Verdict: ")
    verdict.append(report.verdict.value.upper(), style=_VERDICT_STYLE[report.verdict])
    if report.score is not None:
        verdict.append(f"   (health score {report.score}/100)")
    out.print(verdict)
    out.print()

    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column(no_wrap=True)
    table.add_column()
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)

    for f in report.findings:
        if f.severity is Severity.OK:
            continue
        table.add_row(_severity_cell(f.severity, out), f.title, _window(f), _value(f))

    ok_titles = [r.title for r in report.results if r.status is Severity.OK]
    if ok_titles:
        table.add_row(_severity_cell(Severity.OK, out), ", ".join(ok_titles), "", "")
    out.print(table)
    _print_quality(report.flight, out)


def _print_quality(flight: Any, out: Console) -> None:
    if not flight.quality:
        return
    out.print()
    out.print(Text("Data quality", style="bold"))
    for q in flight.quality:
        out.print(Text(f"  · {q.message}", style="dim"))


def _severity_cell(sev: Severity, out: Console) -> Text:
    icon, plain, label, style = _SEVERITY_STYLE[sev]
    return Text(f"{icon if _supports_icons(out) else plain} {label}", style=style)


def _window(f: Finding) -> str:
    if f.start is None:
        return ""
    if f.end is None or f.end - f.start < 1:
        return format_time(f.start)
    return f"{format_time(f.start)}–{format_time(f.end)}"


def _value(f: Finding) -> str:
    if f.measured is None:
        return ""
    unit = f" {f.unit}" if f.unit else ""
    text = f"{fmt_number(f.measured)}{unit}"
    if f.threshold is not None:
        text += f" {f.comparison} {fmt_number(f.threshold)}"
    return text


def fmt_number(x: float) -> str:
    """Readable engineering numbers: 15,869 · 43.4 · 3.52 · 0.84."""
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.0f}"
    if ax >= 100:
        return f"{x:.0f}"
    if ax >= 10 or x == int(x):
        return f"{x:.1f}".rstrip("0").rstrip(".")
    return f"{x:.2f}"


def main() -> None:
    """Entry point. Remaps argument errors to exit code 64.

    Click (bundled inside Typer) uses exit code 2 for bad arguments, which would
    be indistinguishable from a Problem verdict in CI.
    """
    try:
        code = app(standalone_mode=False)
    except typer.Abort:
        sys.exit(130)
    except Exception as exc:
        # ClickException, duck-typed so we don't import Typer's private click copy.
        show = getattr(exc, "show", None)
        exit_code = getattr(exc, "exit_code", None)
        if not callable(show) or not isinstance(exit_code, int):
            # An internal error must never look like a verdict (exit 1 = attention).
            traceback.print_exc()
            err_console.print(f"[red]internal error:[/] {exc}")
            sys.exit(EXIT_CANNOT_ANALYZE)
        show()
        sys.exit(EXIT_USAGE if exit_code == 2 else exit_code)
    sys.exit(code if isinstance(code, int) else 0)
