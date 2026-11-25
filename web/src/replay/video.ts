// Videos attached to a flight, and the arithmetic that links their clock to the log's.
//
// One relation links the two clocks: log time = offset + rate × video time.
// `rate` is 1 unless two or more sync points show the camera's clock runs
// slightly fast or slow.

export type SyncMethod = "none" | "metadata" | "manual" | "points";

export interface Video {
  id: string;
  flight_id: string;
  filename: string;
  file: string;
  sha256: string;
  size_bytes: number | null;
  duration_s: number | null;
  width: number | null;
  height: number | null;
  codec: string | null;
  fps: number | null;
  created_at_media: string | null;
  offset_s: number | null;
  rate: number;
  sync_method: SyncMethod;
  sync_points: [number, number][];
  label: string | null;
  uploaded_at: number;
}

export const isSynced = (v: Video) => v.offset_s != null;

export function videoToLog(v: Pick<Video, "offset_s" | "rate">, videoTime: number): number {
  return (v.offset_s ?? 0) + v.rate * videoTime;
}

export function logToVideo(v: Pick<Video, "offset_s" | "rate">, logTime: number): number {
  return (logTime - (v.offset_s ?? 0)) / v.rate;
}

/** The log times the video covers, or null when not synced or its length is unknown. */
export function coverage(v: Video): [number, number] | null {
  if (!isSynced(v) || !v.duration_s) return null;
  return [videoToLog(v, 0), videoToLog(v, v.duration_s)];
}

export interface Fit {
  offset: number;
  rate: number;
  /** Root-mean-square residual of the points, in seconds (0 for one point). */
  rms: number;
}

/** Offset (one point) or offset and rate (two or more), least squares on log = a + b·video. */
export function fitSync(points: [number, number][]): Fit | null {
  if (points.length === 0) return null;
  if (points.length === 1) {
    const [v, l] = points[0];
    return { offset: l - v, rate: 1, rms: 0 };
  }
  const n = points.length;
  const mv = points.reduce((s, p) => s + p[0], 0) / n;
  const ml = points.reduce((s, p) => s + p[1], 0) / n;
  let sxx = 0, sxy = 0;
  for (const [v, l] of points) {
    sxx += (v - mv) ** 2;
    sxy += (v - mv) * (l - ml);
  }
  if (sxx < 1e-6) {
    // all points at the same video time: only the offset can be known
    return { offset: ml - mv, rate: 1, rms: Math.sqrt(points.reduce((s, [v, l]) => s + (l - (ml - mv) - v) ** 2, 0) / n) };
  }
  const rate = sxy / sxx;
  const offset = ml - rate * mv;
  const rms = Math.sqrt(points.reduce((s, [v, l]) => s + (l - offset - rate * v) ** 2, 0) / n);
  return { offset, rate, rms };
}

/** Rates outside this range mean the points do not belong to the same moments. */
export const RATE_LIMITS: [number, number] = [0.95, 1.05];

/** First guess from the camera's clock: the video starts this many seconds after the log. */
export function creationTimeGuess(createdIso: string | null, logStartIso: string | null): number | null {
  if (!createdIso || !logStartIso) return null;
  const a = Date.parse(createdIso), b = Date.parse(logStartIso);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  const offset = (a - b) / 1000;
  return Math.abs(offset) < 24 * 3600 ? offset : null; // a day off: not the same flight
}

/** How the offset was found, and how far to trust it. */
export function syncLabel(v: Video): string {
  switch (v.sync_method) {
    case "points": {
      const fit = fitSync(v.sync_points);
      const n = v.sync_points.length;
      const acc = fit && n > 1 ? `, ±${Math.round(fit.rms * 1000)} ms between them` : "";
      const drift = Math.abs(v.rate - 1) > 1e-6 ? `, clock ${((v.rate - 1) * 100).toFixed(2)}% fast/slow corrected` : "";
      return `Synced from ${n} point${n === 1 ? "" : "s"}${acc}${drift}`;
    }
    case "manual":
      return "Synced by hand";
    case "metadata":
      return "Guessed from the camera's clock: check it against a sharp moment";
    default:
      return "Not synced yet";
  }
}

// --- API -------------------------------------------------------------------------------

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof msg === "string" ? msg : res.statusText);
  }
  return res.json() as Promise<T>;
}

export const videoApi = {
  list: (flightId: string) =>
    fetch(`/api/flights/${flightId}/videos`).then((r) => json<{ videos: Video[] }>(r)).then((d) => d.videos),
  patch: (id: string, changes: Partial<Pick<Video, "offset_s" | "rate" | "sync_method" | "sync_points" | "label" | "duration_s">>) =>
    fetch(`/api/videos/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }).then((r) => json<{ video: Video }>(r)).then((d) => d.video),
  remove: (id: string) => fetch(`/api/videos/${id}`, { method: "DELETE" }).then((r) => json<{ deleted: string }>(r)),
  fileUrl: (id: string) => `/api/videos/${id}/file`,
  /** Upload with progress (0-1); resolves with the stored video. */
  upload(flightId: string, file: File, onProgress?: (f: number) => void): Promise<{ video: Video; duplicate: boolean }> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/flights/${flightId}/videos`);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
      xhr.onload = () => {
        let body: { detail?: string; video?: Video; duplicate?: boolean } = {};
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* not JSON */
        }
        if (xhr.status >= 200 && xhr.status < 300 && body.video) resolve({ video: body.video, duplicate: !!body.duplicate });
        else reject(new Error(body.detail ?? `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error("Upload failed: the connection was lost"));
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  },
};
