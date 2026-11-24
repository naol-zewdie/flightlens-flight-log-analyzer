import { useState } from "react";
import { Link } from "react-router-dom";
import type { FlightEvent } from "../api";
import { SevIcon } from "../components/Severity";
import { fmtTime } from "../format";
import { useFlight } from "./FlightPage";

const FILTERS: { label: string; match: (e: FlightEvent) => boolean }[] = [
  { label: "All", match: () => true },
  { label: "Modes & arming", match: (e) => ["mode", "arm", "disarm"].includes(e.kind) },
  { label: "Errors & failsafes", match: (e) => e.kind === "error" || e.kind === "failsafe" },
  { label: "Messages", match: (e) => e.kind === "message" },
];

const KIND: Record<FlightEvent["kind"], string> = {
  mode: "Mode",
  arm: "Armed",
  disarm: "Disarmed",
  error: "Error",
  failsafe: "Failsafe",
  message: "Message",
};

export function EventsView() {
  const { flight, events, cursor, setCursor } = useFlight();
  const [f, setF] = useState(0);
  const shown = events.filter(FILTERS[f].match);
  return (
    <>
      <div className="toolbar">
        <div className="segmented" role="group" aria-label="Filter events">
          {FILTERS.map((x, i) => (
            <button key={x.label} aria-pressed={f === i} onClick={() => setF(i)}>
              {x.label}
              <span className="faint" style={{ marginLeft: 6 }}>{events.filter(x.match).length}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="card table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th className="r">Time</th>
              <th>Type</th>
              <th>Event</th>
              <th>Source</th>
              <th><span className="sr-only">Replay</span></th>
            </tr>
          </thead>
          <tbody>
            {shown.map((e, i) => {
              const sev = e.level === "critical" ? "problem" : e.level === "error" || e.level === "warning" ? "attention" : null;
              const active = cursor != null && Math.abs(cursor - e.t) < 0.05;
              return (
                <tr key={i} className="clickable" onClick={() => setCursor(e.t)} style={active ? { background: "var(--accent-tint)" } : undefined}>
                  <td className="r num">{fmtTime(e.t)}</td>
                  <td>
                    {sev ? (
                      <span className={`sev ${sev === "problem" ? "sev-problem" : "sev-attention"}`}>
                        <SevIcon severity={sev} size={14} />
                        {KIND[e.kind]}
                      </span>
                    ) : (
                      <span className="muted">{KIND[e.kind]}</span>
                    )}
                  </td>
                  <td>{e.text}</td>
                  <td className="cell-sub mono">{e.source}</td>
                  <td className="r">
                    <Link className="replay-link" to={`/flights/${flight.id}/replay?t=${Math.max(0, e.t - 5).toFixed(1)}`}
                      onClick={(ev) => ev.stopPropagation()} title="Open the replay 5 s before this event">
                      Replay
                    </Link>
                  </td>
                </tr>
              );
            })}
            {shown.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">No events of this type.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
