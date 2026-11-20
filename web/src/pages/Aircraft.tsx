import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import uPlot from "uplot";
import { api, type AircraftDetail, type AircraftSummary, type MetricSeries } from "../api";
import { useReadOnly } from "../components/server";
import { SevIcon, VerdictPill } from "../components/Severity";
import { Thresholds } from "../components/Thresholds";
import { cssVar, useThemeVersion } from "../components/theme";
import { fmtDate, fmtNum } from "../format";

export function aircraftName(a: { label: string | null; hardware: string | null; serials: string[] }): string {
  if (a.label) return a.label;
  const sn = a.serials[0];
  return `${a.hardware ?? "Autopilot"}${sn ? ` · …${sn.slice(-6)}` : ""}`;
}

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

function airTime(min: number) {
  return min >= 60 ? `${fmtNum(min / 60)} h` : `${fmtNum(min)} min`;
}

// --- list ------------------------------------------------------------------------

export function AircraftList() {
  const readOnly = useReadOnly();
  const [data, setData] = useState<{ aircraft: AircraftSummary[]; unassigned: number; stale: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [queued, setQueued] = useState<number | null>(null);
  const load = useCallback(() => {
    api.aircraft().then(setData).catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  return (
    <main className="page">
      <div className="page-head">
        <div>
          <h1>Aircraft</h1>
          <div className="muted" style={{ marginTop: 6 }}>
            Flights grouped by aircraft name, or by autopilot board · health over time
          </div>
        </div>
      </div>
      {error && <div className="card card-body sev-problem">Cannot reach the FlightLens server: {error}</div>}
      {data && data.stale > 0 && (
        <div className="card card-body notice">
          <span>
            {plural(data.stale, "flight")} {data.stale === 1 ? "was" : "were"} analyzed by an older version and
            {data.stale === 1 ? " has" : " have"} no trend data yet.
          </span>
          {!readOnly &&
            (queued == null ? (
              <button
                className="btn"
                onClick={() =>
                  api.reanalyzeStale().then((r) => {
                    setQueued(r.queued);
                    setTimeout(load, 4000);
                  })
                }
              >
                Re-analyze them
              </button>
            ) : (
              <span className="muted">{plural(queued, "flight")} queued</span>
            ))}
        </div>
      )}
      {!data ? (
        !error && <div className="empty">Loading…</div>
      ) : data.aircraft.length === 0 ? (
        <div className="card empty">
          No aircraft yet. Flights are grouped automatically when the log carries the autopilot's board id, or
          when you name the aircraft on a flight's “Data &amp; provenance” page.
        </div>
      ) : (
        <div className="aircraft-grid">
          {data.aircraft.map((a) => (
            <Link key={a.key} to={`/aircraft/${encodeURIComponent(a.key)}`} className="card aircraft-card">
              <div className="aircraft-card-head">
                <div style={{ minWidth: 0 }}>
                  <div className="cell-main">{aircraftName(a)}</div>
                  <div className="cell-sub">
                    {[a.vehicle_type, a.label ? a.hardware : null].filter(Boolean).join(" · ") || "—"}
                  </div>
                </div>
                <VerdictPill verdict={a.latest_verdict} />
              </div>
              <div className="aircraft-card-stats">
                <span><b className="num">{a.flights}</b> {a.flights === 1 ? "flight" : "flights"}</span>
                <span><b className="num">{airTime(a.airborne_min)}</b> in the air</span>
                <span className="muted">last {fmtDate(a.last)}</span>
              </div>
              {a.alerts.length > 0 ? (
                <div className="sev sev-attention" style={{ fontSize: 13 }}>
                  <SevIcon severity="attention" /> {a.alerts.map((x) => x.title.replace(" has drifted", "")).join(", ")} drifted
                </div>
              ) : (
                <div className="faint" style={{ fontSize: 13 }}>
                  {a.flights > 3 ? "No drift against its own history" : "Trends start after 4 flights"}
                </div>
              )}
            </Link>
          ))}
        </div>
      )}
      {data && data.unassigned > 0 && (
        <p className="hint" style={{ marginTop: 16 }}>
          {plural(data.unassigned, "flight")} cannot be grouped: the log has no board id (for example a simulator).
          Name the aircraft on the flight's “Data &amp; provenance” page to include it.
        </p>
      )}
    </main>
  );
}

// --- one aircraft ------------------------------------------------------------------

export function AircraftPage() {
  const { key = "" } = useParams();
  const navigate = useNavigate();
  const readOnly = useReadOnly();
  const [a, setA] = useState<AircraftDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");

  const load = useCallback(() => {
    api
      .aircraftDetail(key)
      .then((d) => {
        setA(d);
        setName(d.label ?? "");
      })
      .catch((e: Error) => setError(e.message));
  }, [key]);
  useEffect(load, [load]);

  if (error) return <main className="page"><div className="card empty">{error}</div></main>;
  if (!a) return <main className="page"><div className="empty">Loading…</div></main>;
  const index = new Map(a.flight_list.map((f, i) => [f.id, i + 1]));
  const charts = a.metrics.filter((m) => m.worse !== 0);

  return (
    <main className="page">
      <div className="crumbs">
        <Link to="/aircraft">Aircraft</Link> / <span className="mono">{a.serials[0]?.slice(-12) ?? a.key}</span>
      </div>
      <div className="flight-head">
        <div>
          <h1>{aircraftName(a)}</h1>
          <div className="meta">
            {a.vehicle_type && <span>{a.vehicle_type}</span>}
            {a.hardware && <span>{a.hardware}</span>}
            {a.serials.map((s) => <span key={s} className="mono">board {s}</span>)}
          </div>
        </div>
        <VerdictPill verdict={a.latest_verdict} />
      </div>
      <div className="stats" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
        <div className="stat"><div className="k">Flights</div><div className="v">{a.flights}</div></div>
        <div className="stat"><div className="k">Time in the air</div><div className="v">{airTime(a.airborne_min)}</div></div>
        <div className="stat"><div className="k">First flight</div><div className="v text">{fmtDate(a.first)}</div></div>
        <div className="stat"><div className="k">Latest flight</div><div className="v text">{fmtDate(a.last)}</div></div>
      </div>

      {a.alerts.length > 0 && (
        <section className="card" style={{ marginTop: 16 }} aria-label="Maintenance">
          <div className="card-head">
            <h2>Maintenance due</h2>
            <span className="hint">the latest flight against this aircraft's own history</span>
          </div>
          <ul className="findings">
            {a.alerts.map((x) => (
              <li key={x.metric} className="finding" style={{ cursor: "default" }}>
                <span className="f-icon sev sev-attention"><SevIcon severity="attention" /></span>
                <span className="f-title">{x.title}</span>
                <span className="f-sub">
                  {x.text}{" "}
                  <Link to={`/flights/${x.flight_id}`} style={{ color: "var(--accent-ink)" }}>Open the flight</Link>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="card" style={{ marginTop: 16 }} aria-label="Health over time">
        <div className="card-head">
          <h2>Health over time</h2>
          <span className="hint">one point per flight, in flight order · dashed: median of earlier flights · click a point to open it</span>
        </div>
        <div className="card-body">
          {charts.length === 0 ? (
            <div className="empty">No trend data yet.</div>
          ) : (
            <div className="trend-grid">
              {charts.map((m) => (
                <Trend key={m.key} m={m} index={index} onOpen={(id) => navigate(`/flights/${id}`)} />
              ))}
            </div>
          )}
        </div>
      </section>

      <Thresholds a={a} readOnly={readOnly} onChanged={load} />

      <section className="card table-wrap" style={{ marginTop: 16 }} aria-label="Flights">
        <div className="card-head"><h2>Flights</h2></div>
        <table className="data" style={{ marginTop: 8 }}>
          <thead>
            <tr><th className="r">#</th><th>Verdict</th><th>Flight</th><th>Date</th><th>Firmware</th><th className="r">Score</th><th /></tr>
          </thead>
          <tbody>
            {a.flight_list.map((f, i) => (
              <tr key={f.id} className="clickable" tabIndex={0} onClick={() => navigate(`/flights/${f.id}`)}
                  onKeyDown={(e) => e.key === "Enter" && navigate(`/flights/${f.id}`)}>
                <td className="r num faint">{i + 1}</td>
                <td><VerdictPill verdict={f.verdict} /></td>
                <td><div className="cell-main">{f.filename}</div></td>
                <td className="muted">{fmtDate(f.start_time, f.uploaded_at)}</td>
                <td className="muted">{f.firmware ?? "—"}</td>
                <td className="r num">{f.score ?? "—"}</td>
                <td className="r" onClick={(e) => e.stopPropagation()}>
                  {i > 0 && (
                    <Link to={`/compare?a=${a.flight_list[i - 1].id}&b=${f.id}`} className="hint"
                      style={{ color: "var(--accent-ink)", whiteSpace: "nowrap" }}
                      title="Overlay with the previous flight, aligned at takeoff">
                      Compare with #{i}
                    </Link>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {!readOnly && (
        <section className="card" style={{ marginTop: 16, maxWidth: 560 }} aria-label="Name">
          <div className="card-head"><h2>Name this aircraft</h2></div>
          <form className="card-body" style={{ display: "flex", gap: 8 }}
            onSubmit={(e) => {
              e.preventDefault();
              const first = a.flight_list[0];
              if (!first) return;
              api.setAircraft(first.id, name, true).then(() => {
                const label = name.trim();
                navigate(label ? `/aircraft/${encodeURIComponent(`label:${label}`)}` : "/aircraft");
              });
            }}>
            <input className="input" style={{ flex: 1 }} placeholder="e.g. Quad-07" value={name}
              onChange={(e) => setName(e.target.value)} aria-label="Aircraft name" />
            <button className="btn" type="submit">Save for all its flights</button>
          </form>
        </section>
      )}
    </main>
  );
}

function Trend({ m, index, onOpen }: { m: MetricSeries; index: Map<string, number>; onOpen: (id: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  const [hover, setHover] = useState<number | null>(null);
  const pts = m.points;
  const last = pts[pts.length - 1];
  const shown = hover != null ? pts[hover] : last;

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const xs = pts.map((p) => index.get(p.flight_id) ?? 0);
    const ys = pts.map((p) => p.value);
    const base = pts.map((p) => p.baseline);
    const accent = cssVar("--series-1");
    const attention = cssVar("--attention");
    const text3 = cssVar("--text-3");
    const grid = cssVar("--line");
    const font = `10px ${cssVar("--font") || "sans-serif"}`;
    const u = new uPlot(
      {
        width: el.clientWidth,
        height: 120,
        padding: [6, 6, 0, 0],
        legend: { show: false },
        cursor: { points: { size: 9, width: 2 }, drag: { x: false, y: false } },
        // every chart spans the same flights, so the small multiples line up
        scales: { x: { time: false, range: () => [0.5, index.size + 0.5] } },
        axes: [
          { stroke: text3, font, grid: { show: false }, ticks: { size: 3, stroke: grid },
            values: (_u, v) => v.map((x) => (Number.isInteger(x) ? `#${x}` : "")), space: 30, size: 22 },
          { stroke: text3, font, grid: { stroke: grid, width: 1 }, ticks: { show: false },
            values: (_u, v) => v.map((x) => fmtNum(x)), size: 40, space: 24 },
        ],
        series: [
          {},
          { stroke: accent, width: 2, points: { show: true, size: 6, fill: accent, stroke: accent } },
          { stroke: text3, width: 1, dash: [4, 4], points: { show: false } },
        ],
        hooks: {
          draw: [
            (p) => {
              // drift: a ring in the status colour around flagged flights
              const ctx = p.ctx;
              const dpr = devicePixelRatio || 1;
              ctx.save();
              ctx.strokeStyle = attention;
              ctx.lineWidth = 2 * dpr;
              pts.forEach((pt, i) => {
                if (!pt.flagged) return;
                ctx.beginPath();
                ctx.arc(p.valToPos(xs[i], "x", true), p.valToPos(ys[i], "y", true), 6 * dpr, 0, 2 * Math.PI);
                ctx.stroke();
              });
              ctx.restore();
            },
          ],
          setCursor: [(p) => setHover(p.cursor.idx ?? null)],
        },
      },
      [xs, ys, base],
      el,
    );
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: 120 }));
    ro.observe(el);
    const click = () => {
      const i = u.cursor.idx;
      if (i != null) onOpen(pts[i].flight_id);
    };
    u.over.addEventListener("click", click);
    return () => {
      ro.disconnect();
      u.over.removeEventListener("click", click);
      u.destroy();
    };
  }, [m, index, theme, onOpen, pts]);

  const unit = m.unit ? ` ${m.unit}` : "";
  return (
    <div className="trend" title={m.meaning}>
      <div className="trend-head">
        <span>{m.label}</span>
        <span className={`num ${shown.flagged ? "sev-attention" : ""}`}>
          {fmtNum(shown.value)}{unit}
          <span className="faint"> · #{index.get(shown.flight_id)}</span>
        </span>
      </div>
      <div ref={box} style={{ cursor: "pointer" }} role="img"
        aria-label={`${m.label} over ${pts.length} flights, latest ${fmtNum(last.value)}${unit}`} />
    </div>
  );
}
