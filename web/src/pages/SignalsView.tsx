import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type SeriesData, type SignalInfo } from "../api";
import { Chart, type Series } from "../components/Chart";
import { useFlight } from "./FlightPage";
import { SERIES_COLORS } from "./Overview";

const SUBSYSTEMS: [string, string][] = [
  ["att", "Attitude"],
  ["rate", "Angular rate"],
  ["vibe", "Vibration"],
  ["mot", "Motors"],
  ["pusher", "Motors"],
  ["ctrl", "Control"],
  ["bat", "Battery"],
  ["power", "Power"],
  ["gps", "GPS"],
  ["pos", "Position"],
  ["ekf", "Estimator"],
  ["rc", "Radio"],
  ["state", "Flight state"],
];
const groupOf = (name: string) => SUBSYSTEMS.find(([p]) => name.split(".")[0] === p)?.[1] ?? "Other";
const DEFAULTS = ["att.roll", "att.des_roll", "vibe.metric", "vibe.z", "bat.volt", "mot.1", "mot.2", "mot.3", "mot.4"];

/** Charts by subsystem and unit, at most four series each (one y axis per chart). */
function chartGroups(names: string[], catalog: Map<string, SignalInfo>): string[][] {
  const by = new Map<string, string[]>();
  for (const n of names) {
    const key = `${groupOf(n)}|${catalog.get(n)?.unit ?? ""}`;
    by.set(key, [...(by.get(key) ?? []), n]);
  }
  const out: string[][] = [];
  for (const list of by.values()) for (let i = 0; i < list.length; i += 4) out.push(list.slice(i, i + 4));
  return out;
}

export function SignalsView() {
  const { flight, report, events, cursor, setCursor } = useFlight();
  const catalog = useMemo(() => new Map(report.signals.map((s) => [s.name, s])), [report]);
  const [params, setParams] = useSearchParams();
  const selected = useMemo(() => {
    const fromUrl = params.get("s")?.split(",").filter((n) => catalog.has(n));
    return fromUrl?.length ? fromUrl : DEFAULTS.filter((n) => catalog.has(n));
  }, [params, catalog]);
  const [filter, setFilter] = useState("");
  const [range, setRange] = useState<[number, number] | null>(null);
  const [data, setData] = useState<Map<string, SeriesData>>(new Map());
  const [error, setError] = useState<string | null>(null);

  // Full resolution on zoom: re-fetch the visible window.
  useEffect(() => {
    if (!selected.length) return;
    let alive = true;
    const id = setTimeout(() => {
      api
        .signals(flight.id, selected, range ?? undefined, 2500)
        .then((r) => {
          if (!alive) return;
          setError(null);
          setData(new Map(r.signals.map((s) => [s.name, s])));
        })
        .catch((err: Error) => alive && setError(err.message));
    }, range ? 150 : 0);
    return () => {
      alive = false;
      clearTimeout(id);
    };
  }, [flight.id, selected, range]);

  const toggle = (name: string) => {
    const next = selected.includes(name) ? selected.filter((n) => n !== name) : [...selected, name];
    setParams(next.length ? { s: next.join(",") } : {}, { replace: true });
  };

  const groups = useMemo(() => {
    const g = new Map<string, SignalInfo[]>();
    for (const s of report.signals) {
      if (filter && !s.name.includes(filter.toLowerCase())) continue;
      const k = groupOf(s.name);
      g.set(k, [...(g.get(k) ?? []), s]);
    }
    return g;
  }, [report.signals, filter]);

  const modeBands = useMemo(() => {
    const modes = events.filter((e) => e.kind === "mode");
    const end = report.source.log_duration_s ?? 0;
    return modes.map((m, i) => ({ start: m.t, end: i + 1 < modes.length ? modes[i + 1].t : end, label: m.text }));
  }, [events, report]);

  // Memoized so a cursor click does not rebuild every chart.
  const charts = useMemo(
    () =>
      chartGroups(selected, catalog).map((names) => ({
        names,
        series: names
          .map((n, i): Series | null => {
            const d = data.get(n);
            if (!d || d.missing || !d.t) return null;
            return { name: n, unit: d.unit, t: d.t, v: d.v!, color: SERIES_COLORS[i % 4], dash: n.includes(".des_") };
          })
          .filter((x): x is Series => x !== null),
      })),
    [selected, catalog, data],
  );
  const domain = useMemo<[number, number] | null>(
    () => (report.source.log_duration_s ? [0, report.source.log_duration_s] : null),
    [report],
  );

  const onZoom = (r: [number, number] | null) =>
    setRange((cur) => (r === null ? null : cur && Math.abs(cur[0] - r[0]) < 1e-3 && Math.abs(cur[1] - r[1]) < 1e-3 ? cur : r));

  return (
    <div className="explorer">
      <aside className="card picker" aria-label="Signals">
        <input className="input" type="search" placeholder="Filter signals" value={filter} onChange={(e) => setFilter(e.target.value)} />
        {[...groups.entries()].map(([g, list]) => (
          <details key={g} open={!!filter || list.some((s) => selected.includes(s.name))}>
            <summary>
              {g}
              <span className="faint">{list.filter((s) => selected.includes(s.name)).length || ""}</span>
            </summary>
            {list.map((s) => (
              <label key={s.name} title={`${s.source} · ${s.samples} samples${s.rate_hz ? ` · ${s.rate_hz} Hz` : ""}`}>
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
          <span>Drag on a chart to zoom all charts (full resolution loads for the zoomed range). Double-click to reset. Shaded bands are flight modes.</span>
          {range && (
            <button className="btn ghost" style={{ height: 24 }} onClick={() => setRange(null)}>
              Reset zoom
            </button>
          )}
        </div>
        {selected.length === 0 && <div className="card empty">Pick signals on the left to plot them.</div>}
        {charts.map(({ names, series }) => {
          return (
            <section key={names.join()} className="card card-body" aria-label={`${groupOf(names[0])} chart`}>
              <div className="eyebrow" style={{ marginBottom: 6 }}>
                {groupOf(names[0])} · {catalog.get(names[0])?.unit || "no unit"}
              </div>
              {series.length ? (
                <Chart series={series} bands={modeBands} marker={cursor} onPick={setCursor} onZoom={onZoom} xRange={range} domain={domain} syncKey={`fl-${flight.id}`} height={180} />
              ) : (
                <div className="empty">{error ? `Could not load signals: ${error}` : "Loading…"}</div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
