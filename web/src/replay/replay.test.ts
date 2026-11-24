import { describe, expect, it } from "vitest";
import { createClock } from "./clock";
import {
  apply,
  bodyToWorld,
  enuToWorld,
  eulerOf,
  locate,
  quatOf,
  slerp,
  splitColumns,
  stateAt,
  valueAt,
  type ReplayMeta,
} from "./data";

const close = (a: number, b: number, eps = 1e-9) => expect(Math.abs(a - b)).toBeLessThan(eps);
const vclose = (a: number[], b: number[], eps = 1e-9) => a.forEach((x, i) => close(x, b[i], eps));

describe("frames", () => {
  it("maps east/north/up to three.js axes", () => {
    expect(enuToWorld(1, 2, 3)).toEqual([1, 3, -2]);
  });

  it("points the nose where the attitude says", () => {
    const fwd: [number, number, number] = [1, 0, 0]; // body forward
    const right: [number, number, number] = [0, 1, 0];
    // level, heading north: forward is north = world −z, right wing is east = +x
    vclose(apply(bodyToWorld(quatOf(0, 0, 0)), fwd), [0, 0, -1]);
    vclose(apply(bodyToWorld(quatOf(0, 0, 0)), right), [1, 0, 0]);
    // heading east
    vclose(apply(bodyToWorld(quatOf(0, 0, 90)), fwd), [1, 0, 0]);
    // 45° nose up: forward climbs (world +y)
    vclose(apply(bodyToWorld(quatOf(0, 45, 0)), fwd), [0, Math.SQRT1_2, -Math.SQRT1_2]);
    // 30° right roll: the right wing goes down (world −y)
    const r = apply(bodyToWorld(quatOf(30, 0, 0)), right);
    close(r[1], -0.5);
    close(r[0], Math.cos(Math.PI / 6));
  });

  it("round-trips Euler angles like the server", () => {
    for (const [r, p, y] of [[0, 0, 0], [30, 0, 0], [0, 45, 0], [0, 0, 90], [10, -20, 170]]) {
      const e = eulerOf(quatOf(r, p, y));
      close(e.roll, r, 1e-9);
      close(e.pitch, p, 1e-9);
      close(e.yaw, y, 1e-9);
    }
  });

  it("slerps along the short arc, across north", () => {
    const e = eulerOf(slerp(quatOf(0, 0, 350), quatOf(0, 0, 10), 0.5));
    close(e.yaw, 0, 1e-9);
    close(eulerOf(slerp(quatOf(0, 0, 0), quatOf(60, 0, 0), 0.5)).roll, 30, 1e-9);
  });
});

function table(rows: Record<string, number[]>, rate = 10): { meta: ReplayMeta; cols: Record<string, Float32Array> } {
  const columns = Object.keys(rows);
  const n = rows[columns[0]].length;
  const flat = new Float32Array(columns.length * n);
  columns.forEach((c, i) => flat.set(rows[c], i * n));
  const meta = {
    replay_version: "1.0.0", rate_hz: rate, start: rows.t[0], end: rows.t[n - 1], rows: n,
    vehicle_type: "Quadrotor", max_gap_s: 1, sources: { position: "x", height: "x", attitude: "x" },
    origin: null, motors: 0, modes: [], columns, extent: { east: null, north: null, up: null }, gaps: [],
  } satisfies ReplayMeta;
  return { meta, cols: splitColumns(meta, flat.buffer) };
}

describe("sampling", () => {
  const q0 = quatOf(0, 0, 0), q1 = quatOf(20, 0, 0);
  const r = table({
    t: [0, 0.1, 0.2, 0.3],
    east: [0, 1, NaN, NaN],
    north: [0, 0, NaN, NaN],
    up: [5, 5, 5, 5],
    qw: [q0[0], q1[0], q1[0], q1[0]],
    qx: [q0[1], q1[1], q1[1], q1[1]],
    qy: [q0[2], q1[2], q1[2], q1[2]],
    qz: [q0[3], q1[3], q1[3], q1[3]],
    valid_pos: [1, 1, 0, 0],
    valid_att: [1, 1, 1, 1],
  });

  it("locates and interpolates", () => {
    expect(locate(r.meta, 0.15)).toEqual({ i: 1, f: expect.closeTo(0.5, 9) });
    close(valueAt(r.meta, r.cols.east, 0.05), 0.5, 1e-6);
    expect(locate(r.meta, -5)).toEqual({ i: 0, f: 0 });
    expect(locate(r.meta, 99).i).toBe(3);
  });

  it("slerps attitude and holds the last position in a gap", () => {
    const s = stateAt(r, 0.05);
    close(eulerOf(s.q!).roll, 10, 1e-4);
    expect(s.posAge).toBe(0);
    const g = stateAt(r, 0.25); // inside the position gap
    expect(g.east).toBe(1);
    expect(g.posAge).toBeGreaterThan(0);
  });

  it("rejects a truncated table", () => {
    expect(() => splitColumns(r.meta, new Float32Array(3).buffer)).toThrow(/incomplete/);
  });
});

describe("clock", () => {
  it("plays at its rate and stops at the end", () => {
    const c = createClock(0, 10);
    c.play();
    c.tick(1);
    expect(c.get().time).toBe(1);
    c.setRate(4);
    c.tick(1);
    expect(c.get().time).toBe(5);
    c.tick(100);
    expect(c.get()).toMatchObject({ time: 10, playing: false });
    c.play(); // at the end, play starts over
    expect(c.get()).toMatchObject({ time: 0, playing: true });
    c.dispose();
  });

  it("steps through the rates and clamps seeks", () => {
    const c = createClock(0, 10);
    c.faster();
    expect(c.get().rate).toBe(2);
    c.slower();
    c.slower();
    expect(c.get().rate).toBe(0.5);
    c.seek(-3);
    expect(c.get().time).toBe(0);
    c.seek(42);
    expect(c.get().time).toBe(10);
  });

  it("loops a range", () => {
    const c = createClock(0, 100, 20);
    c.setLoop([20, 30]);
    c.play();
    c.tick(12);
    close(c.get().time, 22);
    expect(c.get().playing).toBe(true);
    c.setLoop([5, 5.1]); // too short to loop: cleared
    expect(c.get().loop).toBeNull();
  });

  it("notifies subscribers", () => {
    const c = createClock(0, 10);
    let n = 0;
    const off = c.subscribe(() => n++);
    c.seek(3);
    off();
    c.seek(4);
    expect(n).toBe(1);
  });
});
