import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { aircraftKey, api, profileLabel, type Finding, type SeriesData } from "../api";
import { Chart, type Series } from "../components/Chart";
import { SevIcon, SevLabel } from "../components/Severity";
import { Timeline } from "../components/Timeline";
import { TrackView } from "../components/TrackView";
import { fmtNum, fmtWindow } from "../format";
import { useFlight } from "./FlightPage";

const ISSUE = new Set(["problem", "attention", "error"]);
export const SERIES_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

export function sevColor(sev: string): string {
  return sev === "problem" ? "var(--problem)" : sev === "attention" ? "var(--attention)" : "var(--accent)";
}

export function Overview() {
  const { flight, report, findings, events, track, cursor, setCursor } = useFlight();
  const issues = findings.filter((f) => ISSUE.has(f.severity));
  const skipped = findings.filter((f) => f.severity === "not_evaluated");
  const passed = report.checks.filter((c) => c.status === "ok");
  const [sel, setSel] = useState(0);
  const selected: Finding | undefined = issues[sel];
  const duration = report.source.log_duration_s ?? report.summary.duration_s ?? 0;

  // Selecting a finding moves every view to its moment.
  useEffect(() => {
    if (selected?.start != null) setCursor(selected.start);
  }, [selected, setCursor]);

  // j / k to move through findings.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input, textarea, [role=slider]")) return;
      if (e.key === "j") setSel((i) => Math.min(issues.length - 1, i + 1));
      if (e.key === "k") setSel((i) => Math.max(0, i - 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [issues.length]);

  // Highlight the finding's stretch of path only when it is a distinct part of the flight.
  const covers = selected?.start != null ? ((selected.end ?? selected.start) - selected.start) / Math.max(duration, 1) : 1;
  const win =
    selected?.start != null && covers < 0.5
      ? { start: selected.start, end: selected.end ?? selected.start, color: sevColor(selected.severity) }
      : null;

  return (
    <>
      <div className="overview">
        <section className="card" aria-label="Findings">
          <div className="card-head">
            <h2>Findings</h2>
            {report.profile ? (
              <Link
                className="chip"
                to={aircraftKey(flight) ? `/aircraft/${encodeURIComponent(aircraftKey(flight)!)}` : "#"}
                title={`Judged with threshold profile ${report.profile.name} (${report.profile.digest}) instead of the defaults`}
              >
                Thresholds: {profileLabel(report.profile)}
              </Link>
            ) : (
              <span className="hint">most serious first · j/k</span>
            )}
          </div>
          {issues.length === 0 ? (
            <div className="card-body muted">
              {report.verdict === "healthy"
                ? "No problems found. Every check that could run passed."
                : "No problems found, but too few checks could run to call this flight healthy."}
            </div>
          ) : (
            <ul className="findings" role="listbox" aria-label="Findings">
              {issues.map((f, i) => (
                <li
                  key={`${f.check_id}-${i}`}
                  className="finding"
                  role="option"
                  aria-selected={i === sel}
                  tabIndex={0}
                  onClick={() => setSel(i)}
                  onKeyDown={(e) => e.key === "Enter" && setSel(i)}
                >
                  <span className={`f-icon sev ${f.severity === "problem" ? "sev-problem" : f.severity === "attention" ? "sev-attention" : "sev-neutral"}`}>
                    <SevIcon severity={f.severity} />
                  </span>
                  <span className="f-title">{f.title}</span>
                  <span className="f-sub">
                    {f.severity === "problem" ? "Problem" : f.severity === "attention" ? "Attention" : "Check error"}
                    {f.start != null && <span className="num"> · {fmtWindow(f.start, f.end)}</span>}
                    {f.measured != null && (
                      <span className="num">
                        {" "}· {fmtNum(f.measured)} {f.unit}
                        {f.threshold != null && ` ${f.comparison} ${fmtNum(f.threshold)}`}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {passed.length > 0 && (
            <>
              <hr className="divider" />
              <div className="group-label eyebrow">Passed</div>
              <div className="passed">
                {passed.map((c) => (
                  <span key={c.id} className="chip ok">✓ {c.title}</span>
                ))}
              </div>
            </>
          )}
          {skipped.length > 0 && (
            <>
              <hr className="divider" />
              <div className="group-label eyebrow">Not evaluated</div>
              <ul className="findings" style={{ paddingTop: 0 }}>
                {skipped.map((f) => (
                  <li key={f.check_id} className="finding" style={{ cursor: "default" }}>
                    <span className="f-icon sev sev-neutral"><SevIcon severity="not_evaluated" /></span>
                    <span className="f-title" style={{ fontWeight: 400 }}>{f.title.replace(/ not evaluated$/, "")}</span>
                    <span className="f-sub">{f.explanation}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </section>

        <div className="right-col">
          {selected ? <Evidence finding={selected} flightId={flight.id} onPick={setCursor} cursor={cursor} /> : null}
          <section className="card" aria-label="Flight path">
            <div className="card-head">
              <h2>Flight path</h2>
              <span className="hint">click the path to move the time cursor</span>
            </div>
            <div className="card-body">
              {track ? <TrackView track={track} marker={cursor} window={win} onPick={setCursor} /> : <div className="empty">No position data.</div>}
            </div>
          </section>
        </div>
      </div>

      <section className="card" style={{ marginTop: 16 }} aria-label="Timeline">
        <div className="card-head">
          <h2>Timeline</h2>
          <span className="hint">modes · events · finding windows — click or drag to scrub</span>
        </div>
        <div className="card-body">
          <Timeline duration={duration} events={events} findings={findings} cursor={cursor} onPick={setCursor} />
        </div>
      </section>
    </>
  );
}

function Evidence({ finding, flightId, cursor, onPick }: { finding: Finding; flightId: string; cursor: number | null; onPick: (t: number) => void }) {
  const { events, report } = useFlight();
  const [data, setData] = useState<SeriesData[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const names = finding.evidence_signals.slice(0, 4);

  useEffect(() => {
    setData(null);
    setError(null);
    if (!names.length) return;
    let alive = true;
    api
      .signals(flightId, names)
      .then((r) => alive && setData(r.signals))
      .catch((err: Error) => alive && setError(err.message));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flightId, names.join(",")]);

  const series: Series[] = useMemo(
    () =>
      (data ?? [])
        .filter((s) => !s.missing && s.t)
        .map((s, i) => ({
          name: s.name,
          unit: s.unit,
          t: s.t!,
          v: s.v!,
          color: SERIES_COLORS[i % 4],
          dash: s.name.includes(".des_"),
        })),
    [data],
  );
  // One chart per unit: a y axis is only meaningful for a single unit.
  const groups = useMemo(() => {
    const by = new Map<string, Series[]>();
    for (const s of series) by.set(s.unit ?? "", [...(by.get(s.unit ?? "") ?? []), s]);
    return [...by.entries()];
  }, [series]);
  // Draw the limit only on the chart whose unit it is in.
  const threshold =
    finding.threshold != null
      ? { value: finding.threshold, label: `${finding.severity === "problem" ? "Problem" : "Attention"} limit ${fmtNum(finding.threshold)} ${finding.unit}`, color: sevColor(finding.severity) }
      : null;
  const win = finding.start != null ? { start: finding.start, end: finding.end ?? finding.start, color: sevColor(finding.severity) } : null;
  const logEnd = report.source.log_duration_s ?? null;
  // Open zoomed on the finding with context around it; double-click shows the whole log.
  const focus = useMemo<[number, number] | null>(() => {
    if (!win || logEnd == null) return null;
    const dur = win.end - win.start;
    const lo = Math.max(0, win.start - Math.max(20, dur * 0.5));
    const hi = Math.min(logEnd, win.end + Math.max(5, dur * 0.5));
    return hi - lo < 0.8 * logEnd ? [lo, hi] : null;
  }, [win?.start, win?.end, logEnd]);
  const domain = useMemo<[number, number] | null>(() => (logEnd != null ? [0, logEnd] : null), [logEnd]);
  const modeBands = useMemo(() => {
    const modes = events.filter((e) => e.kind === "mode");
    const end = report.source.log_duration_s ?? 0;
    return modes.map((m, i) => ({ start: m.t, end: i + 1 < modes.length ? modes[i + 1].t : end, label: m.text }));
  }, [events, report]);

  return (
    <section className="card" aria-label="Selected finding">
      <div className="card-body detail">
        <div>
          <SevLabel severity={finding.severity} />
          <h2 style={{ fontSize: 18, marginTop: 6 }}>{finding.title}</h2>
        </div>
        <p className="lede" style={{ margin: 0 }}>{finding.explanation}</p>
        <div className="kv">
          {finding.measured != null && (
            <span><span>Measured</span><span className="num">{fmtNum(finding.measured)} {finding.unit}</span></span>
          )}
          {finding.threshold != null && (
            <span><span>Limit</span><span className="num">{finding.comparison} {fmtNum(finding.threshold)} {finding.unit}</span></span>
          )}
          {finding.start != null && (
            <span><span>When</span><span className="num">{fmtWindow(finding.start, finding.end)}</span></span>
          )}
          <span><span>Check</span><span className="mono">{finding.check_id}</span></span>
        </div>
        {finding.recommendation && (
          <div className="advice">
            <div className="eyebrow" style={{ marginBottom: 2 }}>What to check</div>
            {finding.recommendation}
          </div>
        )}
        {names.length > 0 ? (
          data ? (
            series.length ? (
              groups.map(([unit, list]) => (
                <div key={unit}>
                  {groups.length > 1 && <div className="eyebrow" style={{ marginBottom: 2 }}>{unit || "no unit"}</div>}
                  <Chart
                    series={list}
                    window={win}
                    threshold={threshold && unit === finding.unit ? threshold : null}
                    bands={modeBands}
                    marker={cursor}
                    onPick={onPick}
                    xRange={focus}
                    domain={domain}
                    syncKey={`ev-${flightId}`}
                    height={groups.length > 1 ? 150 : 230}
                  />
                </div>
              ))
            ) : (
              <div className="empty">The evidence signals are not available.</div>
            )
          ) : (
            <div className="empty">{error ? `Could not load the evidence: ${error}` : "Loading evidence…"}</div>
          )
        ) : null}
        {names.length > 0 && (
          <div className="hint">
            Evidence: {names.map((n, i) => (
              <span key={n}>
                {i > 0 && ", "}
                <Link className="mono" to={`signals?s=${encodeURIComponent(names.join(","))}`} style={{ color: "var(--accent-ink)" }}>{n}</Link>
              </span>
            ))}
            {" · drag to zoom, double-click to reset"}
          </div>
        )}
      </div>
    </section>
  );
}
