import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { SevLabel } from "../components/Severity";
import { fmtBytes, fmtTime } from "../format";
import { useReadOnly } from "../components/server";
import { useFlight } from "./FlightPage";

export function QualityView() {
  const { flight, report, findings, reload } = useFlight();
  const navigate = useNavigate();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [label, setLabel] = useState(flight.aircraft ?? "");
  const [busy, setBusy] = useState(false);
  const readOnly = useReadOnly();
  const reasons = new Map(findings.filter((f) => f.severity === "not_evaluated").map((f) => [f.check_id, f.explanation]));
  const src = report.source;

  return (
    <div style={{ display: "grid", gap: 16, gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)" }} className="quality-grid">
      <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
        <section className="card">
          <div className="card-head"><h2>Data quality</h2></div>
          <div className="card-body">
            {report.data_quality.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>No data-quality issues were found while reading this log.</p>
            ) : (
              <ul className="quality">
                {report.data_quality.map((q, i) => (
                  <li key={i}>
                    <span className="chip" style={{ alignSelf: "start", whiteSpace: "nowrap" }}>{q.kind.replace(/_/g, " ")}</span>
                    <span>
                      {q.message}
                      {q.t != null && <span className="faint num"> · {fmtTime(q.t)}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
        <section className="card table-wrap">
          <div className="card-head"><h2>Checks</h2><span className="hint">what ran, and why some could not</span></div>
          <table className="data" style={{ marginTop: 8 }}>
            <thead>
              <tr>
                <th>Check</th>
                <th>Result</th>
                <th>Version</th>
              </tr>
            </thead>
            <tbody>
              {report.checks.map((c) => (
                <tr key={c.id}>
                  <td>
                    <div>{c.title}</div>
                    <div className="cell-sub mono">{c.id}</div>
                    {reasons.get(c.id) && <div className="cell-sub">{reasons.get(c.id)}</div>}
                  </td>
                  <td><SevLabel severity={c.status} /></td>
                  <td className="mono muted">{c.version}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
        <section className="card">
          <div className="card-head"><h2>Provenance</h2></div>
          <div className="card-body">
            <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "8px 16px", margin: 0, fontSize: 13 }}>
              <dt className="faint">File</dt><dd style={{ margin: 0, overflowWrap: "anywhere" }}>{flight.filename} · {fmtBytes(flight.size_bytes)}</dd>
              <dt className="faint">SHA-256</dt><dd className="mono" style={{ margin: 0, overflowWrap: "anywhere", fontSize: 12 }}>{src.sha256}</dd>
              <dt className="faint">Format</dt><dd style={{ margin: 0 }}>{src.format}</dd>
              <dt className="faint">Firmware</dt><dd style={{ margin: 0 }}>{src.firmware ?? "—"}</dd>
              <dt className="faint">Hardware</dt><dd style={{ margin: 0 }}>{src.hardware ?? "—"}</dd>
              <dt className="faint">Log length</dt><dd className="num" style={{ margin: 0 }}>{fmtTime(src.log_duration_s)}</dd>
              <dt className="faint">FlightLens</dt><dd className="mono" style={{ margin: 0 }}>{report.flightlens_version}</dd>
              <dt className="faint">Signal mapping</dt><dd className="mono" style={{ margin: 0 }}>{src.mapping_version ?? "—"}</dd>
              <dt className="faint">Signals</dt><dd style={{ margin: 0 }}>{report.signals.length}</dd>
            </dl>
            <p className="hint" style={{ marginBottom: 0 }}>
              Thresholds are community starting values, not limits approved for this aircraft.
            </p>
          </div>
        </section>
        <section className="card">
          <div className="card-head"><h2>Export</h2></div>
          <div className="card-body" style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <a className="btn" href={`/api/flights/${flight.id}/report.json`}>Report (JSON)</a>
            <a className="btn" href={`/api/flights/${flight.id}/original`}>Original log</a>
          </div>
        </section>
        {!readOnly && (
        <section className="card">
          <div className="card-head"><h2>Manage</h2></div>
          <div className="card-body" style={{ display: "grid", gap: 12 }}>
            <form
              style={{ display: "flex", gap: 8 }}
              onSubmit={(e) => {
                e.preventDefault();
                api.setAircraft(flight.id, label).then(reload);
              }}
            >
              <input className="input" style={{ flex: 1 }} placeholder="Aircraft label, e.g. Quad-07" value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Aircraft label" />
              <button className="btn" type="submit">Save</button>
            </form>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button
                className="btn"
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  api.reanalyze(flight.id).then(() => reload()).finally(() => setBusy(false));
                }}
              >
                Re-analyze
              </button>
              {confirmDelete ? (
                <>
                  <button className="btn danger" onClick={() => api.remove(flight.id).then(() => navigate("/"))}>
                    Delete permanently
                  </button>
                  <button className="btn ghost" onClick={() => setConfirmDelete(false)}>Cancel</button>
                </>
              ) : (
                <button className="btn ghost danger" onClick={() => setConfirmDelete(true)}>Delete flight…</button>
              )}
            </div>
          </div>
        </section>
        )}
      </div>
    </div>
  );
}
