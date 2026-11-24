// Replay data: loading the table and sampling it at any time.
//
// The server builds one table resampled to a uniform clock (see
// src/flightlens/replay.py and docs/replay.md). Values between grid points
// are interpolated here: linearly for numbers, by slerp for the attitude
// quaternion. A NaN means "no data at this time"; it is never turned into 0.

export interface ReplayMeta {
  replay_version: string;
  rate_hz: number;
  start: number;
  end: number;
  rows: number;
  vehicle_type: string | null;
  max_gap_s: number;
  sources: { position: string | null; height: string | null; attitude: string | null };
  origin: { lat: number; lon: number; chosen: string; alt_amsl?: number | null } | null;
  motors: number;
  modes: string[];
  columns: string[];
  extent: Record<"east" | "north" | "up", [number, number] | null>;
  gaps: [number, number][];
}

export type Columns = Record<string, Float32Array>;

export interface Replay {
  meta: ReplayMeta;
  cols: Columns;
}

export class ReplayUnavailable extends Error {}

/** Metadata and the binary table (float32 columns in `meta.columns` order). */
export async function loadReplay(id: string, signal?: AbortSignal): Promise<Replay> {
  const res = await fetch(`/api/flights/${id}/replay`, { signal });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* not JSON */
    }
    throw new ReplayUnavailable(msg);
  }
  const meta = (await res.json()) as ReplayMeta;
  const bin = await fetch(`/api/flights/${id}/replay.bin`, { signal });
  if (!bin.ok) throw new ReplayUnavailable(bin.statusText);
  return { meta, cols: splitColumns(meta, await bin.arrayBuffer()) };
}

export function splitColumns(meta: ReplayMeta, buffer: ArrayBuffer): Columns {
  const all = new Float32Array(buffer);
  if (all.length !== meta.rows * meta.columns.length) {
    throw new ReplayUnavailable("The replay data is incomplete; re-analyze the flight");
  }
  const cols: Columns = {};
  meta.columns.forEach((name, i) => {
    cols[name] = all.subarray(i * meta.rows, (i + 1) * meta.rows);
  });
  return cols;
}

/** Index of the grid sample at or before `t`, and the fraction to the next one. */
export function locate(meta: ReplayMeta, t: number): { i: number; f: number } {
  const x = (t - meta.start) * meta.rate_hz;
  if (!(x > 0)) return { i: 0, f: 0 };
  const last = meta.rows - 1;
  if (x >= last) return { i: last, f: 0 };
  const i = Math.floor(x);
  return { i, f: x - i };
}

/** A column's value at time `t` (linear between samples); NaN where there is no data. */
export function valueAt(meta: ReplayMeta, col: Float32Array | undefined, t: number): number {
  if (!col) return NaN;
  const { i, f } = locate(meta, t);
  const a = col[i];
  if (f === 0 || i + 1 >= col.length) return a;
  const b = col[i + 1];
  if (Number.isNaN(a) || Number.isNaN(b)) return f < 0.5 ? a : b;
  return a + (b - a) * f;
}

/** A discrete column (mode, fix, armed) at `t`: the sample at or before it. */
export function heldAt(meta: ReplayMeta, col: Float32Array | undefined, t: number): number {
  if (!col) return NaN;
  return col[locate(meta, t).i];
}

export type Quat = [number, number, number, number]; // w, x, y, z

export function slerp(a: Quat, b: Quat, f: number): Quat {
  let dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
  let bb = b;
  if (dot < 0) {
    dot = -dot;
    bb = [-b[0], -b[1], -b[2], -b[3]];
  }
  let ka: number, kb: number;
  if (dot > 0.9995) {
    ka = 1 - f;
    kb = f;
  } else {
    const th = Math.acos(Math.min(1, dot));
    const s = Math.sin(th);
    ka = Math.sin((1 - f) * th) / s;
    kb = Math.sin(f * th) / s;
  }
  const q: Quat = [0, 1, 2, 3].map((k) => ka * a[k] + kb * bb[k]) as Quat;
  const n = Math.hypot(...q);
  return q.map((x) => x / n) as Quat;
}

/** The aircraft's state at `t`. In a gap it is the last state with data
 *  (`stale` tells how old), so the model stops instead of jumping. */
export interface State {
  t: number;
  east: number;
  north: number;
  up: number;
  q: Quat | null;
  posAge: number;
  attAge: number;
}

function lastValid(valid: Float32Array | undefined, i: number): number {
  if (!valid) return -1;
  for (let k = i; k >= 0; k--) if (valid[k] > 0) return k;
  return -1;
}

export function stateAt(r: Replay, t: number): State {
  const { meta, cols } = r;
  const { i, f } = locate(meta, t);
  const j = Math.min(i + 1, meta.rows - 1);
  const dt = 1 / meta.rate_hz;

  let east = NaN, north = NaN, up = NaN, posAge = Infinity;
  const pv = cols.valid_pos;
  if (pv && pv[i] > 0 && pv[j] > 0) {
    east = valueAt(meta, cols.east, t);
    north = valueAt(meta, cols.north, t);
    posAge = 0;
  } else {
    const k = lastValid(pv, i);
    if (k >= 0) {
      east = cols.east[k];
      north = cols.north[k];
      posAge = (i - k) * dt + f * dt;
    }
  }
  const upNow = valueAt(meta, cols.up, t);
  up = Number.isFinite(upNow) ? upNow : (() => {
    for (let k = i; k >= 0; k--) if (Number.isFinite(cols.up?.[k])) return cols.up[k];
    return NaN;
  })();

  let q: Quat | null = null, attAge = Infinity;
  const av = cols.valid_att;
  const qa = (k: number): Quat => [cols.qw[k], cols.qx[k], cols.qy[k], cols.qz[k]];
  if (av && av[i] > 0 && av[j] > 0) {
    q = f === 0 ? qa(i) : slerp(qa(i), qa(j), f);
    attAge = 0;
  } else {
    const k = lastValid(av, i);
    if (k >= 0) {
      q = qa(k);
      attAge = (i - k) * dt + f * dt;
    }
  }
  return { t, east, north, up, q, posAge, attAge };
}

// --- frames ------------------------------------------------------------------------------

/** Local east/north/up (metres) to three.js world axes: x = east, y = up, z = south. */
export function enuToWorld(east: number, north: number, up: number): [number, number, number] {
  return [east, up, -north];
}

/** Body (forward, right, down) to NED rotation from a body→NED quaternion; row-major 3×3. */
export function bodyToNed(q: Quat): number[] {
  const [w, x, y, z] = q;
  return [
    1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y),
    2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x),
    2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y),
  ];
}

/** Body (forward, right, down) to three.js world rotation; row-major 3×3.
 *  World = M · R_nb, with M taking north → −z, east → +x, down → −y. */
export function bodyToWorld(q: Quat): number[] {
  const r = bodyToNed(q);
  const row = (k: number) => [r[3 * k], r[3 * k + 1], r[3 * k + 2]];
  const [n, e, d] = [row(0), row(1), row(2)];
  return [...e, ...d.map((v) => -v), ...n.map((v) => -v)];
}

/** Apply a row-major 3×3 matrix to a vector. */
export function apply(m: number[], v: [number, number, number]): [number, number, number] {
  return [
    m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
    m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
    m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
  ];
}

/** Roll, pitch, yaw in degrees (Z-Y-X), the same convention as the server. */
export function eulerOf(q: Quat): { roll: number; pitch: number; yaw: number } {
  const [w, x, y, z] = q;
  const d = 180 / Math.PI;
  return {
    roll: Math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)) * d,
    pitch: Math.asin(Math.max(-1, Math.min(1, 2 * (w * y - z * x)))) * d,
    yaw: Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)) * d,
  };
}

export function quatOf(roll: number, pitch: number, yaw: number): Quat {
  const k = Math.PI / 360;
  const [cr, sr] = [Math.cos(roll * k), Math.sin(roll * k)];
  const [cp, sp] = [Math.cos(pitch * k), Math.sin(pitch * k)];
  const [cy, sy] = [Math.cos(yaw * k), Math.sin(yaw * k)];
  return [
    cr * cp * cy + sr * sp * sy,
    sr * cp * cy - cr * sp * sy,
    cr * sp * cy + sr * cp * sy,
    cr * cp * sy - sr * sp * cy,
  ];
}
