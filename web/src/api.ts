// Typed client for the FlightLens API (see src/flightlens/api.py).

export type Verdict = "healthy" | "attention" | "problem" | "insufficient data";
export type Severity = "ok" | "attention" | "problem" | "not_evaluated" | "error";

export interface FlightRow {
  id: string;
  sha256: string;
  filename: string;
  log_format: string | null;
  size_bytes: number | null;
  uploaded_at: number;
  status: "queued" | "processing" | "done" | "failed";
  error: string | null;
  firmware: string | null;
  vehicle_type: string | null;
  hardware: string | null;
  start_time: string | null;
  duration_s: number | null;
  verdict: Verdict | null;
  score: number | null;
  n_problem: number | null;
  n_attention: number | null;
  aircraft: string | null;
}

export interface Summary {
  duration_s: number | null;
  airborne_s: number | null;
  sorties: { start: number; end: number }[];
  distance_m: number | null;
  max_altitude_m: number | null;
  max_speed_m_s: number | null;
  battery_used_mah: number | null;
  modes: string[];
}

export interface SignalInfo {
  name: string;
  unit: string;
  source: string;
  samples: number;
  rate_hz: number | null;
}

export interface Report {
  flightlens_version: string;
  source: {
    path: string | null;
    sha256: string | null;
    format: string;
    vehicle_type: string | null;
    firmware: string | null;
    hardware: string | null;
    start_time: string | null;
    log_duration_s: number | null;
    mapping_version: string | null;
  };
  verdict: Verdict;
  score: number | null;
  summary: Summary;
  data_quality: { kind: string; message: string; t: number | null }[];
  checks: { id: string; title: string; version: string; status: Severity }[];
  signals: SignalInfo[];
  segments: { start: number; end: number }[];
}

export interface Finding {
  check_id: string;
  severity: Severity;
  title: string;
  explanation: string;
  recommendation: string;
  measured: number | null;
  threshold: number | null;
  comparison: ">" | "<";
  unit: string;
  start: number | null;
  end: number | null;
  evidence_signals: string[];
}

export interface FlightEvent {
  t: number;
  kind: "mode" | "arm" | "disarm" | "error" | "failsafe" | "message";
  text: string;
  level: "info" | "warning" | "error" | "critical";
  source: string;
}

export interface SeriesData {
  name: string;
  unit?: string;
  source?: string;
  samples?: number;
  t?: number[];
  v?: (number | null)[];
  missing?: boolean;
}

export interface Track {
  source: string | null;
  datum?: string;
  t: number[];
  lat: number[];
  lon: number[];
  alt: (number | null)[];
}

export interface Job {
  id: string;
  flight_id: string;
  status: "queued" | "processing" | "done" | "failed";
  error: string | null;
}

export interface Spectrum {
  source: string;
  fs: number;
  air_s: number;
  freqs: number[];
  psd_db: Record<"x" | "y" | "z", number[]>;
  peak_hz: number | null;
  peak_db: number;
  peak_rms: number;
  peak_axis: string | null;
  harmonics: number[];
  tracking: [number, number][];
  tracks_throttle: boolean | null;
  hover_throttle: number | null;
  hover_hz: number | null;
  spectrogram: { t: number[]; f: number[]; db: (number | null)[][] };
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, init);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : msg;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

export const api = {
  flights: (q?: string, verdict?: string) => {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (verdict) p.set("verdict", verdict);
    return call<{ flights: FlightRow[] }>(`/flights?${p}`);
  },
  flight: (id: string) => call<{ flight: FlightRow; report: Report | null }>(`/flights/${id}`),
  findings: (id: string) => call<{ findings: Finding[] }>(`/flights/${id}/findings`),
  events: (id: string) =>
    call<{ events: FlightEvent[]; segments: { start: number; end: number }[] }>(`/flights/${id}/events`),
  track: (id: string) => call<Track>(`/flights/${id}/track`),
  spectrum: (id: string) => call<{ spectrum: Spectrum | null }>(`/flights/${id}/spectrum`),
  signals: (id: string, names: string[], range?: [number, number], maxPoints = 2000) => {
    const p = new URLSearchParams({ names: names.join(","), max_points: String(maxPoints) });
    if (range) {
      p.set("start", String(range[0]));
      p.set("end", String(range[1]));
    }
    return call<{ signals: SeriesData[] }>(`/flights/${id}/signals?${p}`);
  },
  job: (id: string) => call<Job>(`/jobs/${id}`),
  remove: (id: string) => call<{ deleted: string }>(`/flights/${id}`, { method: "DELETE" }),
  reanalyze: (id: string) => call<{ job_id: string }>(`/flights/${id}/reanalyze`, { method: "POST" }),
  setAircraft: (id: string, aircraft: string, sameBoard = false) =>
    call<{ flight: FlightRow; updated: number }>(`/flights/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ aircraft, same_board: sameBoard }),
    }),
  aircraft: () => call<{ aircraft: AircraftSummary[]; unassigned: number; stale: number }>("/aircraft"),
  aircraftDetail: (key: string) => call<AircraftDetail>(`/aircraft/${encodeURIComponent(key)}`),
  reanalyzeStale: () => call<{ queued: number }>("/reanalyze-stale", { method: "POST" }),
};

export interface DriftAlert {
  metric: string;
  title: string;
  text: string;
  flight_id: string;
  value: number;
  baseline: number;
}

export interface AircraftSummary {
  key: string;
  label: string | null;
  serials: string[];
  hardware: string | null;
  vehicle_type: string | null;
  flights: number;
  first: string | null;
  last: string | null;
  latest_verdict: Verdict | null;
  latest_score: number | null;
  airborne_min: number;
  alerts: DriftAlert[];
}

export interface TrendPoint {
  flight_id: string;
  when: string | null;
  value: number;
  baseline: number | null;
  flagged: boolean;
}

export interface MetricSeries {
  key: string;
  label: string;
  unit: string;
  worse: number;
  meaning: string;
  points: TrendPoint[];
}

export interface AircraftDetail extends AircraftSummary {
  metrics: MetricSeries[];
  flight_list: {
    id: string;
    filename: string;
    start_time: string | null;
    uploaded_at: number;
    verdict: Verdict | null;
    score: number | null;
    firmware: string | null;
    serial: string | null;
  }[];
}

/** Upload with progress. Resolves with the API's response body. */
export function uploadLog(
  file: File,
  onProgress: (fraction: number) => void,
): Promise<{ flight_id: string; job_id: string | null; duplicate: boolean; status: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/logs");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as never);
      else {
        const detail = (body as { detail?: unknown } | null)?.detail;
        reject(new ApiError(xhr.status, typeof detail === "string" ? detail : xhr.statusText));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error"));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}
