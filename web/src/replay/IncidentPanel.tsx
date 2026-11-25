// Incident mode: a crash or Problem finding, the likely causes FlightLens linked
// to it (in time order), the critical window looped, and a debrief to start from it.

import { useNavigate } from "react-router-dom";
import type { Finding } from "../api";
import { SevIcon } from "../components/Severity";
import { fmtTime, fmtWindow } from "../format";
import type { Clock } from "./clock";
import { notesApi, rememberedAuthor } from "./notes";

/** Seconds before the incident the replay opens at, and the looped critical window. */
export const INCIDENT_LEAD_S = 60;
export const WINDOW_BEFORE_S = 15;
export const WINDOW_AFTER_S = 5;

export interface Incident {
  finding: Finding;
  /** The linked likely causes, with their findings when known, in time order. */
  causes: { title: string; finding: Finding | null }[];
}

/** The incident for a finding key, with its causes matched to findings by title. */
export function incidentFor(key: string | null, findings: Finding[]): Incident | null {
  if (!key) return null;
  const finding = findings.find((f) => f.key === key);
  if (!finding || finding.start == null) return null;
  const causes = (finding.related ?? []).map((title) => ({
    title,
    finding: findings.find((f) => f.title === title && f !== finding) ?? null,
  }));
  causes.sort((a, b) => (a.finding?.start ?? Infinity) - (b.finding?.start ?? Infinity));
  return { finding, causes };
}

export function criticalWindow(f: Finding, start: number, end: number): [number, number] {
  return [Math.max(start, f.start! - WINDOW_BEFORE_S), Math.min(end, (f.end ?? f.start!) + WINDOW_AFTER_S)];
}

/** The debrief text an incident starts with. */
export function incidentSummary(i: Incident): string {
  const f = i.finding;
  const lines = [`${f.title} at ${fmtWindow(f.start, f.end)}.`];
  if (i.causes.length) {
    lines.push("", "Likely causes found by FlightLens, in order:");
    i.causes.forEach((c, k) =>
      lines.push(`${k + 1}. ${c.title}${c.finding?.start != null ? ` (${fmtTime(c.finding.start)})` : " (throughout the flight)"}`),
    );
  }
  return lines.join("\n");
}

export function IncidentPanel({ incident, clock, flightId, readOnly, onExit }: {
  incident: Incident;
  clock: Clock;
  flightId: string;
  readOnly: boolean;
  onExit: () => void;
}) {
  const navigate = useNavigate();
  const f = incident.finding;
  const startDebrief = async () => {
    const existing = await notesApi.debrief(flightId).catch(() => null);
    if (!existing) {
      await notesApi.saveDebrief(flightId, {
        status: "open",
        summary: incidentSummary(incident),
        conclusion: "",
        actions: [],
        author: rememberedAuthor() || null,
      });
    }
    navigate(`/flights/${flightId}/debrief`);
  };
  return (
    <section className="card card-body incident" aria-label="Incident">
      <div className="incident-head">
        <span className={`sev ${f.severity === "problem" ? "sev-problem" : "sev-attention"}`}><SevIcon severity={f.severity} /></span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="eyebrow">Incident · looping {WINDOW_BEFORE_S} s before to {WINDOW_AFTER_S} s after</div>
          <h2>{f.title} <span className="num faint">{fmtWindow(f.start, f.end)}</span></h2>
          <p className="muted">{f.explanation}</p>
        </div>
        <button className="btn ghost" onClick={() => clock.seek(f.start! - 3)}>Go to it</button>
        {!readOnly && <button className="btn primary" onClick={startDebrief}>Start a debrief</button>}
        <button className="btn ghost" onClick={onExit} aria-label="Leave incident mode">✕</button>
      </div>
      {incident.causes.length > 0 ? (
        <>
          <div className="eyebrow" style={{ marginTop: 10 }}>Likely causes, in time order</div>
          <ol className="causes">
            {incident.causes.map((c, i) => (
              <li key={i}>
                {c.finding?.start != null ? (
                  <button className="link-like" onClick={() => clock.seek(c.finding!.start! - 2)}>
                    <span className="num faint">{fmtWindow(c.finding.start, c.finding.end)}</span> {c.title}
                  </button>
                ) : (
                  <span><span className="faint">throughout</span> {c.title}</span>
                )}
              </li>
            ))}
          </ol>
        </>
      ) : (
        <p className="hint">FlightLens found nothing earlier in this flight that is a likely cause.</p>
      )}
    </section>
  );
}
