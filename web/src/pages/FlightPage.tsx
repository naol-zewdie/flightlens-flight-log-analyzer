import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useOutletContext, useParams } from "react-router-dom";
import { useReadOnly } from "../components/server";
import { api, type Finding, type FlightEvent, type FlightRow, type Report, type Track } from "../api";
import { SevIcon, VERDICT_LABEL, verdictClass } from "../components/Severity";
import { fmtDate, fmtDuration, fmtNum } from "../format";

export interface FlightCtx {
  flight: FlightRow;
  report: Report;
  findings: Finding[];
  events: FlightEvent[];
  track: Track | null;
  /** Shared time cursor (seconds since log start) */
  cursor: number | null;
  setCursor: (t: number | null) => void;
  reload: () => void;
}

export const useFlight = () => useOutletContext<FlightCtx>();

export function FlightPage() {
  const readOnly = useReadOnly();
  const { id = "" } = useParams();
  const [flight, setFlight] = useState<FlightRow | null>(null);
  const [data, setData] = useState<Omit<FlightCtx, "cursor" | "setCursor" | "reload" | "flight"> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState<string[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const current = useRef(id);

  // A different flight: clear the previous one so it is never shown under this URL.
  useEffect(() => {
    current.current = id;
    setFlight(null);
    setData(null);
    setError(null);
    setCursor(null);
  }, [id]);

  const load = useCallback(async () => {
    try {
      const { flight, report, stale } = await api.flight(id);
      if (current.current !== id) return;
      setFlight(flight);
      setStale(stale ?? []);
      setError(null);
      if (flight.status !== "done" || !report) {
        setData(null);
        return;
      }
      const [f, e, t] = await Promise.all([api.findings(id), api.events(id), api.track(id).catch(() => null)]);
      if (current.current !== id) return;
      setData({ report, findings: f.findings, events: e.events, track: t });
    } catch (err) {
      if (current.current === id) setError((err as Error).message);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while queued or processing.
  useEffect(() => {
    if (!flight || (flight.status !== "queued" && flight.status !== "processing")) return;
    const t = setInterval(load, 1000);
    return () => clearInterval(t);
  }, [flight, load]);

  if (error) return <main className="page empty">{error === "No flight with this id" ? "This flight does not exist (it may have been deleted)." : error}</main>;
  if (!flight) return <main className="page empty">Loading…</main>;

  const r = data?.report;
  const verdict = flight.verdict;
  return (
    <main className="page">
      <div className="crumbs">
        <Link to="/">Flights</Link>
        <span>/</span>
        <span className="mono">{flight.id}</span>
      </div>
      <div className="flight-head">
        <div style={{ minWidth: 0 }}>
          <h1 style={{ overflowWrap: "anywhere" }}>{flight.filename}</h1>
          <div className="meta">
            {flight.aircraft && <span>{flight.aircraft}</span>}
            {flight.vehicle_type && <span>{flight.vehicle_type}</span>}
            {flight.hardware && <span>{flight.hardware}</span>}
            {flight.firmware && <span>{flight.firmware}</span>}
            <span>{fmtDate(flight.start_time, flight.uploaded_at)}</span>
            {flight.status === "done" && (
              <a href={`/api/flights/${flight.id}/report.html`} target="_blank" rel="noopener" style={{ color: "var(--accent-ink)" }}
                title="Printable report with evidence charts and provenance; print it to save a PDF">
                Report
              </a>
            )}
            {flight.status === "done" && (
              <Link to={`/compare?a=${flight.id}`} style={{ color: "var(--accent-ink)" }}
                title="Overlay with another flight (by default the previous flight of this aircraft)">
                Compare…
              </Link>
            )}
          </div>
        </div>
        {flight.status === "done" && verdict && (
          <div className={`verdict ${verdictClass(verdict)}`}>
            <div className={`v-label sev ${verdict === "problem" ? "sev-problem" : verdict === "attention" ? "sev-attention" : verdict === "healthy" ? "sev-ok" : "sev-neutral"}`}>
              <SevIcon severity={verdict === "insufficient data" ? "not_evaluated" : verdict} size={20} />
              {VERDICT_LABEL[verdict]}
            </div>
            <div className="v-score" title="Score = 100 − 10 per Attention − 30 per Problem (minimum 0)">
              {flight.score != null ? (
                <>
                  Health score <span className="num" style={{ color: "var(--text)" }}>{flight.score}</span>/100
                </>
              ) : (
                "Not enough data to judge this flight"
              )}
            </div>
          </div>
        )}
      </div>

      {flight.status === "done" && stale.length > 0 && (
        <div className="card card-body notice" style={{ marginTop: 16, marginBottom: 0 }} role="status">
          <span style={{ minWidth: 0 }}>
            <b>Analyzed by an older version of FlightLens.</b>{" "}
            <span className="muted">{stale.slice(0, 4).join("; ")}{stale.length > 4 ? ` and ${stale.length - 4} more` : ""}.</span>
          </span>
          {!readOnly && (
            <button className="btn" onClick={() => api.reanalyze(id).then(load)}>
              Re-analyze
            </button>
          )}
        </div>
      )}

      {flight.status === "queued" || flight.status === "processing" ? (
        <div className="card card-body" style={{ marginTop: 24 }}>
          <h2>Analyzing…</h2>
          <p className="muted">Parsing the log and running every check. This usually takes a few seconds.</p>
          <div className="progress indeterminate" style={{ width: 240 }}><div /></div>
        </div>
      ) : flight.status === "failed" ? (
        <div className="card card-body" style={{ marginTop: 24 }}>
          <h2 className="sev sev-problem"><SevIcon severity="problem" /> This log could not be analyzed</h2>
          <p>{flight.error}</p>
          {!readOnly && <button className="btn" onClick={() => api.reanalyze(id).then(load)}>Try again</button>}
        </div>
      ) : r && data ? (
        <>
          <Stats report={r} />
          <nav className="tabs">
            <NavLink to="" end>Overview</NavLink>
            <NavLink to="replay">Replay</NavLink>
            <NavLink to="debrief">Debrief</NavLink>
            <NavLink to="signals">Signals</NavLink>
            <NavLink to="tuning">Tuning</NavLink>
            <NavLink to="spectrum">Noise spectrum</NavLink>
            <NavLink to="events">Events<span className="count">{data.events.length}</span></NavLink>
            <NavLink to="quality">
              Data &amp; provenance
              {r.data_quality.length > 0 && <span className="count">{r.data_quality.length}</span>}
            </NavLink>
          </nav>
          <Outlet context={{ flight, ...data, cursor, setCursor, reload: load } satisfies FlightCtx} />
        </>
      ) : (
        <div className="empty">Loading…</div>
      )}
    </main>
  );
}

function Stats({ report }: { report: Report }) {
  const s = report.summary;
  const has = (n: string) => report.signals.some((x) => x.name === n);
  const why = {
    distance: has("pos.lat") || has("gps.lat") ? "No position while armed" : "No position data in this log",
    alt: has("pos.rel_alt") ? "No altitude while armed" : "No altitude data in this log",
    speed: has("pos.vx") || has("gps.speed") ? "No velocity while armed" : "No velocity data in this log",
    battery: has("bat.used_mah") ? "No battery data while armed" : "This log has no consumed-capacity data",
  };
  return (
    <div className="stats">
      <Stat
        k="Time in the air"
        v={s.airborne_s != null && s.sorties.length ? fmtDuration(s.airborne_s) : null}
        unit={s.sorties.length > 1 ? `${s.sorties.length} sorties` : undefined}
        why={
          s.duration_s === 0
            ? "Never armed"
            : s.airborne_s != null
              ? `Armed ${fmtDuration(s.duration_s)} but never left the ground`
              : "No attitude or altitude data"
        }
      />
      <Stat k="Distance" v={s.distance_m != null ? (s.distance_m >= 1000 ? fmtNum(s.distance_m / 1000) : fmtNum(s.distance_m)) : null} unit={s.distance_m != null && s.distance_m >= 1000 ? "km" : "m"} why={why.distance} />
      <Stat k="Max altitude" v={fmtNumOrNull(s.max_altitude_m)} unit="m" why={why.alt} />
      <Stat k="Max speed" v={fmtNumOrNull(s.max_speed_m_s)} unit="m/s" why={why.speed} />
      <Stat k="Battery used" v={fmtNumOrNull(s.battery_used_mah)} unit="mAh" why={why.battery} />
      <div className="stat">
        <div className="k">Flight modes</div>
        <div className="v text" title={s.modes.join(", ")}>{s.modes.length ? s.modes.join(", ") : "—"}</div>
      </div>
    </div>
  );
}

const fmtNumOrNull = (x: number | null) => (x == null ? null : fmtNum(x));

function Stat({ k, v, unit, why }: { k: string; v: string | null; unit?: string; why?: string }) {
  return (
    <div className="stat">
      <div className="k">{k}</div>
      {v == null ? (
        <div className="v text faint" title={why}>
          — <span style={{ fontSize: 12 }}>{why}</span>
        </div>
      ) : (
        <div className="v num">
          {v}
          {unit && <small>{unit}</small>}
        </div>
      )}
    </div>
  );
}
