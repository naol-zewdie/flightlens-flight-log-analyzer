import { useEffect, useState } from "react";
import { api, type AircraftDetail } from "../api";
import { fmtNum } from "../format";

const CHECK_LABEL: Record<string, string> = {
  "vibration.level": "Vibration",
  "motor.imbalance": "Motor balance",
  "battery.sag": "Battery voltage",
};

/** Display unit and scale for a threshold, e.g. imbalance is stored as a fraction. */
const UNIT: Record<string, [string, number]> = {
  "vibration.level": ["m/s²", 1],
  "motor.imbalance": ["%", 100],
  "battery.sag": ["V/cell", 1],
};

function settingLabel(key: string): string {
  const px4 = key.startsWith("px4_");
  const base = key.replace(/^px4_/, "").replace(/_/g, " ");
  return `${base.charAt(0).toUpperCase()}${base.slice(1)}${px4 ? " (PX4)" : ""}`;
}

function fmt(check: string, v: number): string {
  const [unit, scale] = UNIT[check] ?? ["", 1];
  return `${fmtNum(v * scale)}${unit ? ` ${unit}` : ""}`;
}

/** The thresholds an aircraft is judged by: defaults, or its own profile. */
export function Thresholds({ a, readOnly, onChanged }: { a: AircraftDetail; readOnly: boolean; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const p = a.profile;

  // While its flights are re-analyzed, refresh until they are done.
  useEffect(() => {
    if (a.pending === 0) return;
    const id = setTimeout(onChanged, 1500);
    return () => clearTimeout(id);
  }, [a, onChanged]);

  const run = (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    action()
      .then(onChanged)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  const rows = p
    ? Object.entries(p.overrides).flatMap(([check, values]) =>
        Object.entries(values).map(([key, value]) => ({
          check,
          key,
          value,
          def: p.defaults[check]?.[key],
          learned: p.basis?.values[`${check}.${key}`],
        })),
      )
    : [];

  return (
    <section className="card" style={{ marginTop: 16 }} aria-label="Thresholds">
      <div className="card-head">
        <h2>Thresholds</h2>
        {p ? (
          <span className="chip" title={`Profile digest ${p.digest}; recorded in every report`}>
            {p.source === "learned" ? `Learned from ${p.basis?.flights ?? "its"} flights` : "Custom"}
          </span>
        ) : (
          <span className="hint">community defaults</span>
        )}
      </div>
      <div className="card-body" style={{ display: "grid", gap: 12 }}>
        {p ? (
          <>
            <div className="muted" style={{ fontSize: 13 }}>
              Profile <b style={{ color: "var(--text)" }}>{p.name}</b>
              {p.notes && ` · ${p.notes}`}. Settings not listed keep their defaults.
            </div>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Setting</th>
                    <th className="r">Default</th>
                    <th className="r">This aircraft</th>
                    {p.source === "learned" && <th className="r">Its median</th>}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={`${r.check}.${r.key}`}>
                      <td>{CHECK_LABEL[r.check] ?? <span className="mono">{r.check}</span>}</td>
                      <td className="muted">{settingLabel(r.key)}</td>
                      <td className="r num muted">{r.def != null ? fmt(r.check, r.def) : "—"}</td>
                      <td className="r num">
                        <b>{fmt(r.check, r.value)}</b>
                        {r.learned?.clamped && (
                          <span className="faint" title="Kept within the safe range around the default"> · limited</span>
                        )}
                      </td>
                      {p.source === "learned" && (
                        <td className="r num muted">{r.learned ? fmt(r.check, r.learned.median) : "—"}</td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <div className="muted" style={{ fontSize: 13, maxWidth: 640 }}>
            Every flight is judged against the default community thresholds. FlightLens can learn
            <i> attention</i> thresholds for vibration, motor balance and battery voltage from this aircraft's own
            flights that had no Problem. Problem thresholds never change, and a learned value never reaches them.
          </div>
        )}

        {a.pending > 0 && (
          <div className="muted" role="status" style={{ fontSize: 13 }}>
            Re-analyzing {a.pending} {a.pending === 1 ? "flight" : "flights"} with these thresholds…
          </div>
        )}
        {error && <div className="sev-problem" style={{ fontSize: 13 }}>{error}</div>}

        {!readOnly && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <button className="btn" disabled={busy} onClick={() => run(() => api.learnProfile(a.key))}>
              {p?.source === "learned" ? "Learn again" : "Learn from its flights"}
            </button>
            {a.profiles.length > 0 && (
              <select
                className="input"
                aria-label="Threshold profile"
                disabled={busy}
                value={p?.name ?? ""}
                onChange={(e) => run(() => api.assignProfile(a.key, e.target.value || null))}
              >
                <option value="">Default thresholds</option>
                {a.profiles.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            )}
            {p && (
              <button className="btn ghost" disabled={busy} onClick={() => run(() => api.assignProfile(a.key, null))}>
                Use defaults
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
