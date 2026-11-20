import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, uploadLog, type FlightRow, type Verdict } from "../api";
import { useReadOnly } from "../components/server";
import { VerdictPill } from "../components/Severity";
import { fmtDate, fmtDuration, plural } from "../format";

interface Upload {
  key: string;
  name: string;
  progress: number;
  phase: "uploading" | "queued" | "processing" | "failed";
  error?: string;
  jobId?: string;
  flightId?: string;
}

const FILTERS: { label: string; value: Verdict | "" }[] = [
  { label: "All", value: "" },
  { label: "Problem", value: "problem" },
  { label: "Attention", value: "attention" },
  { label: "Healthy", value: "healthy" },
  { label: "Insufficient data", value: "insufficient data" },
];
const ACCEPT = ".ulg,.bin,.tlog";

export function Library() {
  const readOnly = useReadOnly();
  const [flights, setFlights] = useState<FlightRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [verdict, setVerdict] = useState<Verdict | "">("");
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const load = useCallback(() => {
    api
      .flights(q || undefined, verdict || undefined)
      .then((r) => {
        setFlights(r.flights);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, [q, verdict]);

  useEffect(() => {
    const id = setTimeout(load, q ? 200 : 0);
    return () => clearTimeout(id);
  }, [load, q]);

  const patch = (key: string, p: Partial<Upload>) =>
    setUploads((us) => us.map((u) => (u.key === key ? { ...u, ...p } : u)));

  const start = useCallback((files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const key = `${file.name}-${file.size}-${Date.now()}-${Math.random()}`;
      setUploads((us) => [{ key, name: file.name, progress: 0, phase: "uploading" }, ...us]);
      uploadLog(file, (p) => patch(key, { progress: p }))
        .then((res) => {
          if (res.duplicate || !res.job_id) {
            setUploads((us) => us.filter((u) => u.key !== key));
            load();
            return;
          }
          patch(key, { phase: "queued", jobId: res.job_id, flightId: res.flight_id });
        })
        .catch((e) => patch(key, { phase: "failed", error: e.message }));
    }
  }, [load]);

  // Poll jobs while anything is being analyzed.
  useEffect(() => {
    const pending = uploads.filter((u) => u.jobId && (u.phase === "queued" || u.phase === "processing"));
    if (!pending.length) return;
    const id = setInterval(async () => {
      for (const u of pending) {
        try {
          const job = await api.job(u.jobId!);
          if (job.status === "done") {
            setUploads((us) => us.filter((x) => x.key !== u.key));
            load();
          } else if (job.status === "failed") {
            patch(u.key, { phase: "failed", error: job.error ?? "Analysis failed" });
            load();
          } else patch(u.key, { phase: job.status });
        } catch {
          /* transient; try again next tick */
        }
      }
    }, 800);
    return () => clearInterval(id);
  }, [uploads, load]);

  // Drop anywhere on the page (not on a read-only demo).
  useEffect(() => {
    if (readOnly) return;
    let depth = 0;
    const enter = (e: DragEvent) => {
      if (!e.dataTransfer?.types.includes("Files")) return;
      depth++;
      setDragging(true);
    };
    const leave = () => {
      depth = Math.max(0, depth - 1);
      if (!depth) setDragging(false);
    };
    const over = (e: DragEvent) => e.preventDefault();
    const drop = (e: DragEvent) => {
      e.preventDefault();
      depth = 0;
      setDragging(false);
      if (e.dataTransfer?.files.length) start(e.dataTransfer.files);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  }, [start, readOnly]);

  const isEmpty = !readOnly && flights !== null && flights.length === 0 && !q && !verdict && uploads.length === 0;

  return (
    <main className="page">
      {dragging && <div className="drag-overlay">Drop flight logs to analyze</div>}
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files?.length) start(e.target.files);
          e.target.value = "";
        }}
      />
      <div className="page-head">
        <div>
          <h1>Flights</h1>
          <div className="muted" style={{ marginTop: 6 }}>
            {flights ? plural(flights.length, "flight") : "Loading…"}
            {verdict || q ? " matching" : " analyzed"}
          </div>
        </div>
        {!readOnly && (
        <button className="btn primary" onClick={() => input.current?.click()}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden>
            <path d="M8 11V2.5M4.5 6L8 2.5 11.5 6M2.5 13.5h11" />
          </svg>
          Upload logs
        </button>
        )}
      </div>

      {isEmpty ? (
        <button className="dropzone big" style={{ width: "100%" }} onClick={() => input.current?.click()}>
          <strong>Drop a flight log here</strong>
          <span>PX4 .ulg and ArduPilot .bin logs are supported. Analysis runs on this machine; nothing is sent anywhere.</span>
          <span className="faint" style={{ marginTop: 8 }}>or click to browse</span>
        </button>
      ) : (
        <>
          <div className="toolbar">
            <input
              className="input"
              type="search"
              placeholder="Search file, firmware, hardware…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              aria-label="Search flights"
            />
            <div className="segmented" role="group" aria-label="Filter by verdict">
              {FILTERS.map((f) => (
                <button key={f.label} aria-pressed={verdict === f.value} onClick={() => setVerdict(f.value)}>
                  {f.label}
                </button>
              ))}
            </div>
          </div>
          {error && <div className="card card-body sev-problem">Cannot reach the FlightLens server: {error}</div>}
          <div className="card table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Verdict</th>
                  <th>Flight</th>
                  <th>Aircraft</th>
                  <th>Firmware</th>
                  <th>Date</th>
                  <th className="r">Duration</th>
                  <th>Findings</th>
                  <th className="r">Score</th>
                </tr>
              </thead>
              <tbody>
                {uploads.map((u) => (
                  <tr key={u.key}>
                    <td>
                      {u.phase === "failed" ? (
                        <span className="pill problem">Failed</span>
                      ) : (
                        <span className="pill neutral">{u.phase === "uploading" ? "Uploading" : "Analyzing"}</span>
                      )}
                    </td>
                    <td colSpan={7}>
                      <div className="cell-main">{u.name}</div>
                      {u.phase === "failed" ? (
                        <div className="cell-sub sev-problem" style={{ display: "flex", gap: 12 }}>
                          {u.error}
                          <button className="btn ghost" style={{ height: 20, padding: "0 6px" }} onClick={() => setUploads((us) => us.filter((x) => x.key !== u.key))}>
                            Dismiss
                          </button>
                        </div>
                      ) : (
                        <div className={`progress ${u.phase !== "uploading" ? "indeterminate" : ""}`} style={{ marginTop: 6 }}>
                          <div style={u.phase === "uploading" ? { width: `${Math.round(u.progress * 100)}%` } : undefined} />
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
                {flights?.map((f) => (
                  <tr
                    key={f.id}
                    className="clickable"
                    tabIndex={0}
                    onClick={() => navigate(`/flights/${f.id}`)}
                    onKeyDown={(e) => e.key === "Enter" && navigate(`/flights/${f.id}`)}
                  >
                    <td>
                      {f.status === "done" ? (
                        <VerdictPill verdict={f.verdict} />
                      ) : f.status === "failed" ? (
                        <span className="pill problem">Failed</span>
                      ) : (
                        <span className="pill neutral">Analyzing</span>
                      )}
                    </td>
                    <td>
                      <div className="cell-main">{f.filename}</div>
                      <div className="cell-sub mono">{f.id}</div>
                    </td>
                    <td>
                      <div>{f.aircraft || f.vehicle_type || "—"}</div>
                      <div className="cell-sub">{f.hardware ?? ""}</div>
                    </td>
                    <td className="muted">{f.firmware ?? "—"}</td>
                    <td className="muted" style={{ whiteSpace: "nowrap" }}>{fmtDate(f.start_time, f.uploaded_at)}</td>
                    <td className="r num">{fmtDuration(f.duration_s)}</td>
                    <td>
                      {f.status === "failed" ? (
                        <span className="cell-sub">{f.error}</span>
                      ) : (
                        <FindingCounts p={f.n_problem ?? 0} a={f.n_attention ?? 0} />
                      )}
                    </td>
                    <td className="r num">{f.score ?? "—"}</td>
                  </tr>
                ))}
                {flights?.length === 0 && uploads.length === 0 && (
                  <tr>
                    <td colSpan={8} className="empty">
                      No flights match.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="hint" style={{ marginTop: 10 }}>
            Drop .ulg or .bin files anywhere on this page to add them.
          </div>
        </>
      )}
    </main>
  );
}

function FindingCounts({ p, a }: { p: number; a: number }) {
  if (!p && !a) return <span className="faint">None</span>;
  return (
    <span style={{ display: "inline-flex", gap: 10 }}>
      {p > 0 && <span className="sev sev-problem">{plural(p, "problem")}</span>}
      {a > 0 && <span className="sev sev-attention">{a} attention</span>}
    </span>
  );
}
