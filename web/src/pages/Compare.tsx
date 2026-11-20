import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  aircraftKey,
  api,
  profileLabel,
  type Finding,
  type FlightRow,
  type MetricSpec,
  type Report,
  type SeriesData,
  type Severity,
} from "../api";
import { Chart, type Series } from "../components/Chart";
import { SevIcon, VerdictPill } from "../components/Severity";
import { fmtDate, fmtDuration, fmtNum, fmtTime } from "../format";
import { chartGroups, DEFAULT_SIGNALS, pickerGroups } from "../signals";
import { SERIES_COLORS } from "./Overview";

interface Side {
  flight: FlightRow;
  report: Report;
  findings: Finding[];
  /** Log time that becomes t = 0: the first takeoff, else arming, else the log start. */
  zero: number;
  zeroIs: "takeoff" | "arming" | "log start";
}

async function loadSide(id: string): Promise<Side> {
  const [{ flight, report }, { findings }] = await Promise.all([api.flight(id), api.findings(id)]);
  if (!report) throw new Error(`${flight.filename} is not analyzed yet`);
  const takeoff = report.summary.sorties[0]?.start;
  const armed = report.segments[0]?.start;
  return takeoff != null
    ? { flight, report, findings, zero: takeoff, zeroIs: "takeoff" }
    : armed != null
      ? { flight, report, findings, zero: armed, zeroIs: "arming" }
      : { flight, report, findings, zero: 0, zeroIs: "log start" };
}

const when = (f: FlightRow) => f.start_time ?? `~${f.uploaded_at.toFixed(3).padStart(20, "0")}`;
const duration = (s: Side) => s.report.source.log_duration_s ?? s.report.summary.duration_s ?? 0;
const ISSUE = new Set<Severity>(["problem", "attention", "error"]);
const RANK: Record<Severity, number> = { problem: 4, attention: 3, error: 2, not_evaluated: 1, ok: 0 };

export function Compare() {
  const [params, setParams] = useSearchParams();
  const aId = params.get("a");
  const bId = params.get("b");
  const [flights, setFlights] = useState<FlightRow[]>([]);
  const [specs, setSpecs] = useState<MetricSpec[]>([]);
  const [A, setA] = useState<Side | null>(null);
  const [B, setB] = useState<Side | null>(null);
  const [error, setError] = useState<string | null>(null);

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) (v ? next.set(k, v) : next.delete(k));
    setParams(next, { replace: true });
  };

  useEffect(() => {
    api.flights().then((r) => setFlights(r.flights.filter((f) => f.status === "done" && f.verdict)));
    api.metricSpecs().then((r) => setSpecs(r.metrics)).catch(() => setSpecs([]));
  }, []);

  // Only one flight given: compare with the previous flight of the same aircraft.
  useEffect(() => {
    if (!aId || bId || !flights.length) return;
    const a = flights.find((f) => f.id === aId);
    const key = a && aircraftKey(a);
    const prev = flights
      .filter((f) => f.id !== aId && key && aircraftKey(f) === key && a && when(f) < when(a))
      .sort((x, y) => (when(x) < when(y) ? 1 : -1))[0];
    if (prev) set({ a: prev.id, b: aId });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aId, bId, flights]);

  useEffect(() => {
    setError(null);
    setA(null);
    if (aId) loadSide(aId).then(setA).catch((e: Error) => setError(e.message));
  }, [aId]);
  useEffect(() => {
    setError(null);
    setB(null);
    if (bId) loadSide(bId).then(setB).catch((e: Error) => setError(e.message));
  }, [bId]);

  return (
    <main className="page">
      <div className="crumbs">
        <Link to="/">Flights</Link> / <span>Compare</span>
      </div>
      <div className="page-head">
        <div>
          <h1>Compare flights</h1>
          <div className="muted" style={{ marginTop: 6 }}>
            Two flights on the same signals, aligned at takeoff (t = 0)
          </div>
        </div>
      </div>
      {error && <div className="card card-body sev-problem" style={{ marginBottom: 16 }}>{error}</div>}

      <div className="compare-heads">
        <FlightCard tag="A" side={A} id={aId} flights={flights} onPick={(id) => set({ a: id })} />
        <FlightCard tag="B" side={B} id={bId} flights={flights} onPick={(id) => set({ b: id })} />
      </div>

      {A && B && (
        <>
          <div className="compare-tables">
            <MetricDeltas a={A} b={B} specs={specs} />
            <FindingsDiff a={A} b={B} />
          </div>
          <Overlay a={A} b={B} />
        </>
      )}
      {(!aId || !bId) && !error && (
        <div className="card empty" style={{ marginTop: 16 }}>Pick two flights to compare.</div>
      )}
    </main>
  );
}

function FlightCard({
  tag,
  side,
  id,
  flights,
  onPick,
}: {
  tag: "A" | "B";
  side: Side | null;
  id: string | null;
  flights: FlightRow[];
  onPick: (id: string) => void;
}) {
  const f = side?.flight;
  const s = side?.report.summary;
  return (
    <section className="card card-body compare-head" aria-label={`Flight ${tag}`}>
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <span className={`compare-tag ${tag === "B" ? "dashed" : ""}`} aria-hidden>
          {tag}
        </span>
        <select
          className="input"
          style={{ flex: 1 }}
          aria-label={`Flight ${tag}`}
          value={id ?? ""}
          onChange={(e) => e.target.value && onPick(e.target.value)}
        >
          <option value="">Choose a flight…</option>
          {flights.map((x) => (
            <option key={x.id} value={x.id}>
              {x.filename} · {fmtDate(x.start_time, x.uploaded_at)}
            </option>
          ))}
        </select>
      </div>
      {f && side && s ? (
        <div style={{ display: "grid", gap: 8, marginTop: 12 }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "baseline" }}>
            <Link to={`/flights/${f.id}`} className="cell-main" style={{ overflowWrap: "anywhere" }}>
              {f.filename}
            </Link>
            <VerdictPill verdict={f.verdict} />
          </div>
          <div className="kv">
            <span><span>Score</span><b className="num">{f.score ?? "—"}</b></span>
            <span><span>In the air</span><span className="num">{fmtDuration(s.airborne_s)}</span></span>
            <span><span>Aircraft</span>{f.aircraft || f.hardware || "—"}</span>
            <span>
              <span>Thresholds</span>
              {side.report.profile ? profileLabel(side.report.profile) : "defaults"}
            </span>
          </div>
          <div className="hint">
            t = 0 is {side.zeroIs === "takeoff" ? "takeoff" : <b>{side.zeroIs} (no takeoff detected)</b>} at {fmtTime(side.zero)} in the log
          </div>
        </div>
      ) : (
        id && <div className="empty">Loading…</div>
      )}
    </section>
  );
}

function MetricDeltas({ a, b, specs }: { a: Side; b: Side; specs: MetricSpec[] }) {
  const ma = JSON.parse(a.flight.metrics ?? "{}") as Record<string, number>;
  const mb = JSON.parse(b.flight.metrics ?? "{}") as Record<string, number>;
  const rows = specs.filter((s) => s.key in ma || s.key in mb);
  return (
    <section className="card table-wrap" aria-label="Health metrics">
      <div className="card-head">
        <h2>Health metrics</h2>
        <span className="hint">B against A</span>
      </div>
      {rows.length === 0 ? (
        <div className="card-body muted">Neither flight has trend metrics.</div>
      ) : (
        <table className="data">
          <thead>
            <tr><th>Metric</th><th className="r">A</th><th className="r">B</th><th className="r">Change</th></tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const x = ma[s.key];
              const y = mb[s.key];
              const raw = x != null && y != null ? y - x : null;
              // a change too small to show at display precision is no change
              const d = raw != null && Number(fmtNum(Math.abs(raw))) === 0 ? 0 : raw;
              const worse = d != null && s.worse !== 0 && d * s.worse > 0;
              const better = d != null && s.worse !== 0 && d * s.worse < 0;
              const unit = s.unit ? ` ${s.unit}` : "";
              return (
                <tr key={s.key}>
                  <td>{s.label}</td>
                  <td className="r num">{x != null ? `${fmtNum(x)}${unit}` : "—"}</td>
                  <td className="r num">{y != null ? `${fmtNum(y)}${unit}` : "—"}</td>
                  <td className={`r num ${worse ? "sev-attention" : better ? "sev-ok" : "muted"}`}>
                    {d == null ? "—" : d === 0 ? "same" : `${d > 0 ? "+" : "−"}${fmtNum(Math.abs(d))}${unit}`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function worst(fs: Finding[]): Finding | undefined {
  return [...fs].sort((x, y) => RANK[y.severity] - RANK[x.severity])[0];
}

function FindingsDiff({ a, b }: { a: Side; b: Side }) {
  const titles = new Map([...a.report.checks, ...b.report.checks].map((c) => [c.id, c.title]));
  const issuesA = a.findings.filter((f) => ISSUE.has(f.severity));
  const issuesB = b.findings.filter((f) => ISSUE.has(f.severity));
  const ids = [...new Set([...issuesA, ...issuesB].map((f) => f.check_id))];
  const status = (s: Side, id: string) => s.report.checks.find((c) => c.id === id)?.status;
  const rows = ids
    .map((id) => {
      const fa = worst(issuesA.filter((f) => f.check_id === id));
      const fb = worst(issuesB.filter((f) => f.check_id === id));
      const kind = fa && fb ? "both" : fb ? "new" : "gone";
      return { id, fa, fb, kind };
    })
    .sort((x, y) => ["new", "both", "gone"].indexOf(x.kind) - ["new", "both", "gone"].indexOf(y.kind));

  const cell = (f: Finding | undefined, s: Side, id: string) =>
    f ? (
      <span className={`sev ${f.severity === "problem" ? "sev-problem" : f.severity === "attention" ? "sev-attention" : "sev-neutral"}`} title={f.explanation}>
        <SevIcon severity={f.severity} /> {f.title}
      </span>
    ) : status(s, id) === "ok" ? (
      <span className="sev sev-ok"><SevIcon severity="ok" /> OK</span>
    ) : (
      <span className="faint">not evaluated</span>
    );

  return (
    <section className="card table-wrap" aria-label="Findings compared">
      <div className="card-head">
        <h2>Findings</h2>
        <span className="hint">new in B first</span>
      </div>
      {rows.length === 0 ? (
        <div className="card-body muted">Neither flight has a problem or attention finding.</div>
      ) : (
        <table className="data">
          <thead>
            <tr><th>Check</th><th>A</th><th>B</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>
                  <div className="cell-main">{titles.get(r.id) ?? r.id}</div>
                  <div className="cell-sub">{r.kind === "new" ? "New in B" : r.kind === "gone" ? "Gone in B" : "In both"}</div>
                </td>
                <td>{cell(r.fa, a, r.id)}</td>
                <td>{cell(r.fb, b, r.id)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Overlay({ a, b }: { a: Side; b: Side }) {
  const [params, setParams] = useSearchParams();
  const catalog = useMemo(() => {
    const inB = new Set(b.report.signals.map((s) => s.name));
    return new Map(a.report.signals.filter((s) => inB.has(s.name)).map((s) => [s.name, s]));
  }, [a, b]);
  const selected = useMemo(() => {
    const fromUrl = params.get("s")?.split(",").filter((n) => catalog.has(n));
    return fromUrl?.length ? fromUrl : DEFAULT_SIGNALS.filter((n) => catalog.has(n)).slice(0, 4);
  }, [params, catalog]);
  const [filter, setFilter] = useState("");
  const [range, setRange] = useState<[number, number] | null>(null);
  const [data, setData] = useState<{ a: Map<string, SeriesData>; b: Map<string, SeriesData> } | null>(null);
  const [cursor, setCursor] = useState<number | null>(0);

  useEffect(() => {
    if (!selected.length) return;
    let alive = true;
    const shift = (s: Side): [number, number] | undefined => (range ? [range[0] + s.zero, range[1] + s.zero] : undefined);
    const id = setTimeout(() => {
      Promise.all([
        api.signals(a.flight.id, selected, shift(a), 2000),
        api.signals(b.flight.id, selected, shift(b), 2000),
      ]).then(([ra, rb]) => {
        if (!alive) return;
        setData({
          a: new Map(ra.signals.map((s) => [s.name, s])),
          b: new Map(rb.signals.map((s) => [s.name, s])),
        });
      });
    }, range ? 150 : 0);
    return () => {
      alive = false;
      clearTimeout(id);
    };
  }, [a, b, selected, range]);

  const toggle = (name: string) => {
    const next = new URLSearchParams(params);
    const list = selected.includes(name) ? selected.filter((n) => n !== name) : [...selected, name];
    if (list.length) next.set("s", list.join(","));
    else next.delete("s");
    setParams(next, { replace: true });
  };

  const groups = useMemo(() => pickerGroups([...catalog.values()], filter), [catalog, filter]);
  const domain = useMemo<[number, number]>(
    () => [Math.min(-a.zero, -b.zero), Math.max(duration(a) - a.zero, duration(b) - b.zero)],
    [a, b],
  );
  // Two series per signal (A solid, B dashed), so two signals per chart.
  const charts = useMemo(
    () =>
      chartGroups(selected, catalog, 2).map((names) => ({
        names,
        series: names.flatMap((n, i): Series[] => {
          const out: Series[] = [];
          for (const [tag, side, d] of [["A", a, data?.a], ["B", b, data?.b]] as const) {
            const s = d?.get(n);
            if (!s || s.missing || !s.t) continue;
            out.push({
              name: `${n} · ${tag}`,
              unit: s.unit,
              t: s.t.map((t) => t - side.zero),
              v: s.v!,
              // own colour per flight, plus a dash for B so colour is never the only cue
              color: SERIES_COLORS[(2 * i + (tag === "B" ? 1 : 0)) % 4],
              dash: tag === "B",
            });
          }
          return out;
        }),
      })),
    [selected, catalog, data, a, b],
  );

  const onZoom = (r: [number, number] | null) =>
    setRange((cur) => (r === null ? null : cur && Math.abs(cur[0] - r[0]) < 1e-3 && Math.abs(cur[1] - r[1]) < 1e-3 ? cur : r));

  return (
    <div className="explorer" style={{ marginTop: 16 }}>
      <aside className="card picker" aria-label="Signals in both flights">
        <input className="input" type="search" placeholder="Filter signals" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {catalog.size === 0 && <div className="empty">The two flights have no signals in common.</div>}
        {[...groups.entries()].map(([g, list]) => (
          <details key={g} open={!!filter || list.some((s) => selected.includes(s.name))}>
            <summary>
              {g}
              <span className="faint">{list.filter((s) => selected.includes(s.name)).length || ""}</span>
            </summary>
            {list.map((s) => (
              <label key={s.name}>
                <input type="checkbox" checked={selected.includes(s.name)} onChange={() => toggle(s.name)} />
                <span className="mono">{s.name}</span>
                <span className="u">{s.unit}</span>
              </label>
            ))}
          </details>
        ))}
      </aside>
      <div className="charts">
        <div className="hint" style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
          <span>
            Solid line: A · dashed line: B · time from takeoff. Drag to zoom both flights, double-click to reset.
          </span>
          {range && (
            <button className="btn ghost" style={{ height: 24 }} onClick={() => setRange(null)}>
              Reset zoom
            </button>
          )}
        </div>
        {selected.length === 0 && <div className="card empty">Pick signals on the left to overlay them.</div>}
        {charts.map(({ names, series }) => (
          <section key={names.join()} className="card card-body" aria-label={`${names.join(", ")} compared`}>
            <div className="eyebrow" style={{ marginBottom: 6 }}>
              {names.join(" · ")} · {catalog.get(names[0])?.unit || "no unit"}
            </div>
            {series.length ? (
              <Chart series={series} marker={cursor} onPick={setCursor} onZoom={onZoom} xRange={range} domain={domain} syncKey="fl-compare" height={200} />
            ) : (
              <div className="empty">Loading…</div>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
