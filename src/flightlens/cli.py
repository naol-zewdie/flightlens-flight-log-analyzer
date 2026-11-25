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
from flightlens.profiles import Profile, ProfileError, learn_profile, load_profile
from flightlens.summary import summarize

EXIT_CANNOT_ANALYZE = 3
EXIT_USAGE = 64
LOG_SUFFIXES = {".ulg", ".bin", ".tlog"}

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Automatic drone flight log analysis.",
)
profile_app = typer.Typer(
    no_args_is_help=True, help="Threshold profiles: per-aircraft threshold overrides."
)
app.add_typer(profile_app, name="profile")
reviews_app = typer.Typer(no_args_is_help=True, help="Engineer reviews of findings.")
app.add_typer(reviews_app, name="reviews")
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

ProfileOption = Annotated[
    Path | None,
    typer.Option(
        "--profile",
        help="Threshold profile (.yaml) to use instead of the default thresholds.",
    ),
]

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


def _profile_or_exit(path: Path | None) -> Profile | None:
    if path is None:
        return None
    try:
        return load_profile(path)
    except OSError as exc:
        err_console.print(f"[red]error:[/] cannot read profile {path}: {exc}")
    except ProfileError as exc:
        err_console.print(f"[red]error:[/] profile {path.name}: {exc}")
    raise typer.Exit(EXIT_USAGE)


@app.command("analyze")
def analyze_cmd(
    log: Annotated[Path, typer.Argument(help="Path to a PX4 .ulg or ArduPilot .bin log.")],
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the report as JSON.")
    ] = None,
    profile_path: ProfileOption = None,
    html_out: Annotated[
        Path | None,
        typer.Option(
            "--html",
            help="Also write a self-contained HTML report (open it and print to save a PDF).",
        ),
    ] = None,
) -> None:
    """Analyze a flight log and print the verdict and findings."""
    from flightlens.htmlreport import flight_reader, render_html

    profile = _profile_or_exit(profile_path)
    flight = _load_or_exit(log)
    report = analyze(flight, profile=profile)
    render_report(report, console)

    outputs: list[tuple[Path | None, str, Any]] = [
        (json_out, "JSON report", lambda: json.dumps(report.to_dict(), indent=2)),
        (
            html_out,
            "HTML report",
            lambda: render_html(report.to_dict(), flight_reader(flight.signals), filename=log.name),
        ),
    ]
    for path, what, make in outputs:
        if path is None:
            continue
        try:
            path.write_text(make(), encoding="utf-8")
        except OSError as exc:
            err_console.print(f"[red]error:[/] cannot write {path}: {exc}")
            raise typer.Exit(EXIT_CANNOT_ANALYZE) from None
        console.print(f"\n{what} written to {path}")

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
    profile_path: ProfileOption = None,
) -> None:
    """Analyze every log in a folder and summarize how the system behaved."""
    profile = _profile_or_exit(profile_path)
    files = _log_files(folder)
    if profile:
        console.print(f"Thresholds: profile {profile.name} ({profile.source}, {profile.digest})")

    rows: list[dict[str, Any]] = []
    not_evaluated: Counter[str] = Counter()
    for path in files:
        row: dict[str, Any] = {"file": path.name, "size_mb": round(path.stat().st_size / 1e6, 1)}
        t0 = time.perf_counter()
        try:
            flight = parsers.load(path)
            report = analyze(flight, profile=profile)
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


def _log_files(folder: Path) -> list[Path]:
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in LOG_SUFFIXES)
    if not files:
        err_console.print(f"[red]error:[/] no .ulg, .bin or .tlog files in {folder}")
        raise typer.Exit(EXIT_CANNOT_ANALYZE)
    return files


@profile_app.command("learn")
def profile_learn_cmd(
    folder: Annotated[
        Path, typer.Argument(help="Folder with one aircraft's logs (searched recursively).")
    ],
    name: Annotated[str, typer.Option("--name", help="Name of the profile.")],
    out: Annotated[Path, typer.Option("--out", help="Write the profile to this .yaml file.")],
) -> None:
    """Learn attention thresholds from one aircraft's own flights.

    Uses only flights without a Problem verdict. Learned values never reach the
    default problem thresholds; problem thresholds are never learned.
    """
    from flightlens.metrics import flight_metrics

    rows: list[dict[str, Any]] = []
    for path in _log_files(folder):
        try:
            flight = parsers.load(path)
        except parsers.ParseError as exc:
            err_console.print(f"[dim]skipped {path.name}: {exc}[/]")
            continue
        report = analyze(flight)
        rows.append(
            {
                "id": path.name,
                "verdict": report.verdict.value,
                "log_format": flight.metadata.log_format,
                "metrics": flight_metrics(flight, report),
            }
        )
    try:
        profile = learn_profile(name, rows, notes=f"Learned from {len(rows)} logs in {folder.name}")
    except ProfileError as exc:
        err_console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(EXIT_CANNOT_ANALYZE) from None
    table = Table(box=None, header_style="bold", pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("Threshold", "Default", "Learned", "Median", "Flights", ""):
        table.add_column(col, justify="left" if col in ("Threshold", "") else "right")
    for key, b in (profile.basis or {}).get("values", {}).items():
        cid, setting = key.rsplit(".", 1)
        table.add_row(
            key,
            f"{b['default']:g}",
            f"{profile.overrides[cid][setting]:g}",
            f"{b['median']:g}",
            str(b["flights"]),
            Text("clamped", style="yellow") if b["clamped"] else "",
        )
    console.print(table)
    try:
        out.write_text(profile.to_yaml(), encoding="utf-8")
    except OSError as exc:
        err_console.print(f"[red]error:[/] cannot write {out}: {exc}")
        raise typer.Exit(EXIT_CANNOT_ANALYZE) from None
    console.print(f"Profile {profile.name} ({profile.digest}) written to {out}")


DataOpt = Annotated[
    Path | None,
    typer.Option("--data", help="Library folder (default: $FLIGHTLENS_DATA or ./data)."),
]


def _data_dir(data: Path | None) -> Path:
    import os

    return data or Path(os.environ.get("FLIGHTLENS_DATA", "data"))


@app.command("backup")
def backup_cmd(
    out: Annotated[Path, typer.Argument(help="Backup file to write (.zip).")],
    data: DataOpt = None,
    no_analysis: Annotated[
        bool,
        typer.Option(
            "--no-analysis",
            help="Leave out analysis results (much smaller; they are rebuilt on restore).",
        ),
    ] = False,
    no_video: Annotated[
        bool,
        typer.Option("--no-video", help="Leave out attached videos (they are not restored)."),
    ] = False,
) -> None:
    """Back up the library: database, original logs, videos and analysis results.

    Safe while `flightlens serve` is running: the database is copied with
    SQLite's online backup, so the copy is consistent.
    """
    from flightlens import backup

    try:
        s = backup.create(
            _data_dir(data), out, include_analysis=not no_analysis, include_video=not no_video
        )
    except backup.BackupError as exc:
        err_console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(EXIT_USAGE) from None
    console.print(
        f"{s.flights} flights"
        + (f" and {s.videos} video{'s' if s.videos != 1 else ''}" if s.videos else "")
        + f" backed up to {s.path} ({s.size_bytes / 1e6:.1f} MB"
        + f"{'' if s.included_analysis else ', without analysis results'}"
        + f"{', without videos' if no_video else ''})"
    )


@app.command("restore")
def restore_cmd(
    backup_file: Annotated[
        Path, typer.Argument(help="Backup file (.zip) from `flightlens backup`.")
    ],
    data: Annotated[
        Path, typer.Option("--data", help="New, empty folder to restore the library into.")
    ],
) -> None:
    """Restore a backup into a new folder (never over an existing library).

    Every original log is checked against its SHA-256 before anything is put
    in place. Start the server on the restored folder with
    `flightlens serve --data <folder>`.
    """
    from flightlens import backup

    try:
        result = backup.restore(
            backup_file, data, progress=lambda name: console.print(f"  re-analyzing {name}")
        )
    except backup.BackupError as exc:
        err_console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(EXIT_USAGE) from None
    m = result["manifest"]
    console.print(
        f"Restored {result['flights']} flights (backup of {m['created']}, FlightLens "
        f"{m['flightlens_version']}) into {data}"
        + (f"; {result['reanalyzed']} re-analyzed" if result["reanalyzed"] else "")
    )


@app.command("prune")
def prune_cmd(
    older_than: Annotated[
        float, typer.Option("--older-than", help="Remove flights older than this many days.")
    ],
    data: DataOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Delete them; without it, only list what would go.")
    ] = False,
    include_reviewed: Annotated[
        bool,
        typer.Option("--include-reviewed", help="Also remove flights an engineer has reviewed."),
    ] = False,
) -> None:
    """Remove old flights from the library (lists them first; deletes only with --yes)."""
    from datetime import UTC, datetime

    from flightlens import retention
    from flightlens.store import Store

    data_dir = _data_dir(data)
    if not (data_dir / "flightlens.db").is_file():
        err_console.print(f"[red]error:[/] {data_dir} is not a FlightLens library")
        raise typer.Exit(EXIT_USAGE)
    store = Store(data_dir)
    chosen = retention.candidates(store, older_than, keep_reviewed=not include_reviewed)
    if not chosen:
        console.print(f"No flights older than {older_than:g} days.")
        return
    for c in chosen:
        when = datetime.fromtimestamp(c.when, tz=UTC).strftime("%Y-%m-%d")
        console.print(f"  {when}  {c.filename}  ({c.size_bytes / 1e6:.1f} MB)")
    total = sum(c.size_bytes for c in chosen) / 1e6
    if not yes:
        console.print(
            f"{len(chosen)} flights ({total:.1f} MB of logs) would be removed. "
            "Run again with --yes to delete them; consider `flightlens backup` first."
        )
        return
    n = retention.prune(store, chosen)
    console.print(f"Removed {n} flights ({total:.1f} MB of logs).")


@reviews_app.command("export")
def reviews_export_cmd(
    out: Annotated[Path, typer.Option("--out", help="Write the manifest to this .yaml file.")],
    data: Annotated[
        Path, typer.Option("--data", help="FlightLens data folder (as for serve).")
    ] = Path("data"),
) -> None:
    """Export engineer reviews as a validation manifest for `flightlens validate`.

    Confirmed and accepted findings become expected findings; false alarms
    become must-not rules. Re-run `flightlens validate` on the file after an
    upgrade to see whether the new version still agrees with your reviews.
    """
    from flightlens.reviews import manifest_yaml
    from flightlens.store import Store

    if not (data / "flightlens.db").is_file():
        err_console.print(f"[red]error:[/] {data} is not a FlightLens data folder")
        raise typer.Exit(EXIT_USAGE)
    store = Store(data)
    rows = store.all_reviews()
    if not rows:
        err_console.print("[red]error:[/] there are no reviews to export")
        raise typer.Exit(EXIT_CANNOT_ANALYZE)
    out.write_text(manifest_yaml(rows, store.root.resolve() / "flights"), encoding="utf-8")
    flights = len({r["flight_id"] for r in rows})
    console.print(f"{len(rows)} reviews of {flights} flights written to {out}")


@profile_app.command("show")
def profile_show_cmd(
    path: Annotated[Path, typer.Argument(help="Profile .yaml file.")],
) -> None:
    """Check a profile file and list what it changes against the defaults."""
    profile = _profile_or_exit(path)
    assert profile is not None
    defaults = {c.id: c.defaults for c in all_checks()}
    console.print(f"{profile.name} · {profile.source} · {profile.digest}")
    for cid, values in profile.overrides.items():
        for key, value in values.items():
            console.print(f"  {cid}.{key}: {defaults[cid][key]:g} → {value:g}")


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


@app.command("validate")
def validate_cmd(
    manifests: Annotated[
        list[Path],
        typer.Argument(help="Label manifests (.yaml), or folders containing them."),
    ],
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write the full results as JSON.")
    ] = None,
    markdown_out: Annotated[
        Path | None, typer.Option("--markdown", help="Write the results as a Markdown report.")
    ] = None,
    title: Annotated[
        str, typer.Option(help="Title of the Markdown report.")
    ] = "Validation results",
    profile_path: ProfileOption = None,
) -> None:
    """Score FlightLens against logs with known outcomes (exit 1 if any log is wrong)."""
    from flightlens import validate

    profile = _profile_or_exit(profile_path)

    files = validate.find_manifests(manifests)
    try:
        cases = [c for m in files for c in validate.load_manifest(m)]
    except (OSError, ValueError, KeyError) as exc:
        err_console.print(f"[red]error:[/] cannot read manifest: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc
    if not cases:
        err_console.print("[red]error:[/] no labelled logs found")
        raise typer.Exit(EXIT_USAGE)

    outcomes = []
    for case in cases:
        o = validate.evaluate(case, config=profile.overrides if profile else None)
        outcomes.append(o)
        mark = Text("PASS ", style="green") if o.passed else Text("FAIL ", style="bold red")
        mark.append(f"{case.id:<44} ")
        mark.append("; ".join(o.problems())[:110], style="" if o.passed else "red")
        console.print(mark)

    table = Table(box=None, header_style="bold", pad_edge=False)
    for col in (
        "Check",
        "Expected",
        "Detected",
        "Severity ok",
        "False alarms",
        "Recall",
        "Precision",
    ):
        table.add_column(col, justify="left" if col == "Check" else "right")
    for check, s in validate.per_check(outcomes).items():
        table.add_row(
            check,
            str(s.expected),
            str(s.detected),
            str(s.exact),
            str(s.false_alarms),
            "-" if s.recall is None else f"{100 * s.recall:.0f}%",
            "-" if s.precision is None else f"{100 * s.precision:.0f}%",
        )
    console.print()
    console.print(table)
    passed = sum(o.passed for o in outcomes)
    console.print(f"\n{passed} of {len(outcomes)} logs fully correct")

    if json_out is not None:
        json_out.write_text(json.dumps(validate.to_dict(outcomes), indent=2), encoding="utf-8")
    if markdown_out is not None:
        markdown_out.write_text(validate.to_markdown(outcomes, title), encoding="utf-8")
    raise typer.Exit(0 if passed == len(outcomes) else 1)


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


@app.command("replay-info")
def replay_info_cmd(
    log: Annotated[Path, typer.Argument(help="Path to a .ulg, .bin or .tlog log.")],
) -> None:
    """What a replay of the log would contain: duration, rate, data sources, gaps."""
    from flightlens.replay import build_replay

    flight = _load_or_exit(log)
    meta = build_replay(flight).meta
    src = meta["sources"]
    origin = meta["origin"]
    console.print(
        f"{log.name} · {flight.metadata.vehicle_type or 'unknown vehicle'} · "
        f"{format_time(meta['end'] - meta['start'])} at {meta['rate_hz']:g} Hz "
        f"({meta['rows']} samples)"
    )
    console.print(f"  position  {src['position'] or 'none: attitude-only replay'}")
    if origin:
        console.print(f"  origin    {origin['chosen']} ({origin['lat']:.6f}, {origin['lon']:.6f})")
    console.print(f"  height    {src['height'] or 'none'}")
    console.print(f"  attitude  {src['attitude'] or 'none: a level model'}")
    console.print(f"  motors    {meta['motors'] or 'none'}")
    console.print(f"  modes     {', '.join(meta['modes']) or 'none'}")
    gaps = meta["gaps"]
    if gaps:
        listed = ", ".join(f"{format_time(a)}-{format_time(b)}" for a, b in gaps[:5])
        more = f" and {len(gaps) - 5} more" if len(gaps) > 5 else ""
        console.print(f"  gaps      {len(gaps)} over {meta['max_gap_s']:g} s: {listed}{more}")
    else:
        console.print("  gaps      none")


HostOpt = Annotated[str, typer.Option(help="Interface to listen on.")]
PortOpt = Annotated[int, typer.Option(help="Port to listen on.")]
WebOpt = Annotated[
    Path | None,
    typer.Option(
        help="Built web UI folder (default: $FLIGHTLENS_WEB, the bundled UI, or web/dist)."
    ),
]
AllowHostOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--allow-host",
        help="Extra host name the server answers to, e.g. a LAN IP (repeatable; "
        "also $FLIGHTLENS_ALLOWED_HOSTS, comma-separated). Default: localhost only.",
    ),
]
ReadOnlyOpt = Annotated[
    bool,
    typer.Option("--read-only", help="Refuse uploads, edits and deletes (for a public demo)."),
]


def default_web_dir() -> Path:
    """$FLIGHTLENS_WEB, else ./web/dist (a source checkout, freshest), else the UI
    bundled in the installed package."""
    import os

    if env := os.environ.get("FLIGHTLENS_WEB"):
        return Path(env)
    local = Path("web/dist")
    if (local / "index.html").exists():
        return local
    return Path(__file__).parent / "web"


WorkersOpt = Annotated[
    int,
    typer.Option(
        min=1,
        max=64,
        envvar="FLIGHTLENS_WORKERS",
        help="Logs analyzed at the same time (each uses one CPU core and its memory).",
    ),
]
LogFormatOpt = Annotated[
    str,
    typer.Option(
        envvar="FLIGHTLENS_LOG_FORMAT",
        help="Server log format: text, or json (one object per line, for a log collector).",
    ),
]
LogLevelOpt = Annotated[
    str,
    typer.Option(envvar="FLIGHTLENS_LOG_LEVEL", help="debug, info, warning or error."),
]
GraceOpt = Annotated[
    float,
    typer.Option(
        "--shutdown-grace",
        min=0,
        envvar="FLIGHTLENS_SHUTDOWN_GRACE",
        help="Seconds running analyses get to finish on shutdown; the rest resume on restart.",
    ),
]


def _serve(
    host: str,
    port: int,
    data_dir: Path,
    web: Path | None,
    allow_host: list[str] | None,
    read_only: bool,
    workers: int = 1,
    log_format: str = "text",
    log_level: str = "info",
    shutdown_grace: float = 30.0,
) -> None:
    import math
    import os

    import uvicorn

    from flightlens.api import LOCAL_HOSTS, create_app
    from flightlens.ops import setup_logging

    try:
        setup_logging(log_format.lower(), log_level.lower())
    except ValueError as exc:
        err_console.print(f"[red]error:[/] {exc}")
        raise typer.Exit(EXIT_USAGE) from None

    hosts = [*LOCAL_HOSTS, *(allow_host or [])]
    hosts += [h.strip() for h in os.environ.get("FLIGHTLENS_ALLOWED_HOSTS", "").split(",")]
    if host not in ("0.0.0.0", "::"):
        hosts.append(host)
    hosts = [h for h in hosts if h]

    if host not in ("127.0.0.1", "localhost", "::1"):
        import logging

        logging.getLogger("flightlens.server").warning(
            "listening beyond this computer with no login: anyone who can reach it can "
            "upload, change and delete flights; keep it on a trusted network (see SECURITY.md)",
            extra={"host": host, "read_only": read_only},
        )
    web_dir = web or default_web_dir()
    if not (web_dir / "index.html").exists():
        err_console.print(f"[yellow]note:[/] no web UI at {web_dir}; serving the API only")
    if log_format.lower() == "text":  # JSON mode: only JSON lines, for the log collector
        mode = " · read-only" if read_only else ""
        console.print(f"FlightLens {__version__} · data in {data_dir.resolve()}{mode}")
        console.print(
            f"Open http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
        )
    application = create_app(
        data_dir,
        web_dir,
        allowed_hosts=hosts,
        read_only=read_only,
        workers=workers,
        shutdown_grace_s=shutdown_grace,
    )
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_config=None,  # setup_logging owns the handlers
        log_level="warning",
        access_log=False,
        # open requests get at most 10 s, then analyses get the grace period, so a
        # stop takes at most grace + 20 s (docker-compose allows 45 s for the default)
        timeout_graceful_shutdown=min(10, math.ceil(shutdown_grace)),
    )


@app.command("serve")
def serve_cmd(
    host: HostOpt = "127.0.0.1",
    port: PortOpt = 8000,
    data: Annotated[
        Path | None, typer.Option(help="Data folder (default: $FLIGHTLENS_DATA or ./data).")
    ] = None,
    web: WebOpt = None,
    allow_host: AllowHostOpt = None,
    read_only: ReadOnlyOpt = False,
    workers: WorkersOpt = 1,
    log_format: LogFormatOpt = "text",
    log_level: LogLevelOpt = "info",
    shutdown_grace: GraceOpt = 30.0,
) -> None:
    """Start the web app and API."""
    import os

    data_dir = data or Path(os.environ.get("FLIGHTLENS_DATA", "data"))
    _serve(
        host,
        port,
        data_dir,
        web,
        allow_host,
        read_only,
        workers=workers,
        log_format=log_format,
        log_level=log_level,
        shutdown_grace=shutdown_grace,
    )


@app.command("demo")
def demo_cmd(
    data: Annotated[
        Path, typer.Option(help="Folder for the demo flights (kept between runs).")
    ] = Path("flightlens-demo"),
    host: HostOpt = "127.0.0.1",
    port: PortOpt = 8000,
    web: WebOpt = None,
    allow_host: AllowHostOpt = None,
    read_only: ReadOnlyOpt = False,
    seed_only: Annotated[
        bool, typer.Option("--seed-only", help="Create the demo flights and exit.")
    ] = False,
) -> None:
    """Generate example flights (each with a known fault) and open them in the web app."""
    from flightlens.demo import seed

    console.print(f"Preparing demo flights in {data.resolve()} ...")
    ids = seed(data, progress=lambda name: console.print(f"  analyzing {name}"))
    console.print(f"{len(ids)} demo flights ready")
    if not seed_only:
        _serve(host, port, data, web, allow_host, read_only)


@app.command("version")
def version_cmd() -> None:
    """Print the FlightLens version."""
    console.print(__version__)


def _time_text(report: Report, summ: Any) -> str | None:
    if report.flight.never_armed:
        log = report.flight.metadata.log_duration
        return "never armed" + (f" (log {format_time(log)})" if log else "")
    if summ.duration_s is None:
        return None
    text = f"armed {format_time(summ.duration_s)}"
    if summ.airborne_s is not None:
        n = len(summ.sorties)
        text += (
            f", in the air {format_time(summ.airborne_s)} ({n} sortie{'s' if n != 1 else ''})"
            if n
            else ", never left the ground"
        )
    return text


def render_report(report: Report, out: Console) -> None:
    md = report.flight.metadata
    summ = summarize(report.flight)
    header = " · ".join(
        p
        for p in (
            f"FlightLens {report.engine_version}",
            md.firmware,
            md.vehicle_type,
            _time_text(report, summ),
            f"profile {report.profile.name}" if report.profile else None,
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
