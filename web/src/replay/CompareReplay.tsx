// Two flights in one 3D scene on one clock: each drawn from its own take-off
// point, aligned in time at take-off, arming, the log start or a chosen event.

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { Link, useSearchParams } from "react-router-dom";
import { api, type FlightEvent, type FlightRow, type Report, type SeriesData } from "../api";
import { Chart, type Series } from "../components/Chart";
import { useThemeVersion } from "../components/theme";
import { fmtTime } from "../format";
import { createClock, RATES, useClock, type Clock } from "./clock";
import { bodyToWorld, enuToWorld, eulerOf, heldAt, loadReplay, stateAt, valueAt, type Replay } from "./data";
import { buildAircraft, color, niceStep } from "./Scene3D";
import { fmtTenths } from "./ReplayView";

interface Side {
  flight: FlightRow;
  report: Report;
  events: FlightEvent[];
  replay: Replay;
}

/** Alignment choices for one flight: label and log time that becomes t = 0. */
function anchors(s: Side): { id: string; label: string; t: number }[] {
  const out: { id: string; label: string; t: number }[] = [];
  const takeoff = s.report.summary.sorties[0]?.start;
  if (takeoff != null) out.push({ id: "takeoff", label: "Take-off", t: takeoff });
  const armed = s.report.segments[0]?.start;
  if (armed != null) out.push({ id: "arming", label: "Arming", t: armed });
  out.push({ id: "start", label: "Log start", t: 0 });
  s.events
    .filter((e) => e.kind === "mode" || e.kind === "failsafe" || e.level === "error" || e.level === "critical")
    .forEach((e, i) => out.push({ id: `e${i}`, label: `${e.kind === "mode" ? "Mode" : "Event"}: ${e.text} (${fmtTime(e.t)})`, t: e.t }));
  return out;
}

async function loadSide(id: string): Promise<Side> {
  const [{ flight, report }, { events }, replay] = await Promise.all([api.flight(id), api.events(id), loadReplay(id)]);
  if (!report) throw new Error(`${flight.filename} is not analyzed yet`);
  return { flight, report, events, replay };
}

const COLORS = ["--series-1", "--series-2"] as const;

export default function CompareReplay() {
  const [params, setParams] = useSearchParams();
  const [sides, setSides] = useState<[Side, Side] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const a = params.get("a"), b = params.get("b");

  useEffect(() => {
    if (!a || !b) return;
    setSides(null);
    Promise.all([loadSide(a), loadSide(b)]).then((s) => setSides(s as [Side, Side]), (e: Error) => setError(e.message));
  }, [a, b]);

  const choice = (k: "za" | "zb", s: Side | undefined) => {
    const list = s ? anchors(s) : [];
    return list.find((x) => x.id === params.get(k)) ?? list[0];
  };
  const za = choice("za", sides?.[0]), zb = choice("zb", sides?.[1]);
  const setChoice = (k: string, v: string) => {
    const next = new URLSearchParams(params);
    next.set(k, v);
    setParams(next, { replace: true });
  };

  return (
    <main className="page">
      <div className="crumbs">
        <Link to="/">Flights</Link> / <Link to={`/compare?a=${a ?? ""}&b=${b ?? ""}`}>Compare</Link> / <span>Replay</span>
      </div>
      <div className="page-head">
        <div>
          <h1>Replay two flights</h1>
          <div className="muted" style={{ marginTop: 6 }}>
            One 3D scene on one clock. Each flight is drawn from its own take-off point; time is aligned at the moments chosen below (t = 0).
          </div>
        </div>
      </div>
      {error && <div className="card card-body sev-problem">{error}</div>}
      {!a || !b ? (
        <div className="card empty">Pick two flights on the <Link to="/compare">Compare</Link> page first.</div>
      ) : !sides ? (
        !error && <div className="empty">Loading both replays…</div>
      ) : (
        <>
          <div className="compare-heads">
            {sides.map((s, i) => {
              const list = anchors(s);
              const cur = i === 0 ? za : zb;
              return (
                <div key={i} className="card card-body compare-side">
                  <div className="eyebrow">
                    <i className="swatch" style={{ background: `var(${COLORS[i]})` }} /> Flight {i ? "B" : "A"}
                    {i ? " · dashed" : " · solid"}
                  </div>
                  <Link to={`/flights/${s.flight.id}/replay`} className="cell-main">{s.flight.filename}</Link>
                  <label className="hint">
                    Align at{" "}
                    <select value={cur?.id} onChange={(e) => setChoice(i ? "zb" : "za", e.target.value)} aria-label={`Align flight ${i ? "B" : "A"} at`}>
                      {list.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
                    </select>
                  </label>
                </div>
              );
            })}
          </div>
          <CompareScreen key={`${a}-${b}-${za?.t}-${zb?.t}`} sides={sides} zero={[za?.t ?? 0, zb?.t ?? 0]} />
        </>
      )}
    </main>
  );
}

function CompareScreen({ sides, zero }: { sides: [Side, Side]; zero: [number, number] }) {
  const start = Math.min(...sides.map((s, i) => s.replay.meta.start - zero[i]));
  const end = Math.max(...sides.map((s, i) => s.replay.meta.end - zero[i]));
  const clock = useMemo(() => createClock(start, end, Math.max(start, -10)), [start, end]);
  useEffect(() => () => clock.dispose(), [clock]);
  const [camera, setCamera] = useState<"top" | "orbit" | "chaseA" | "chaseB">("orbit");
  const playing = useClock(clock, (s) => s.playing);
  const rate = useClock(clock, (s) => s.rate);
  const time = useClock(clock, (s) => s.time);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input, select, textarea")) return;
      const k = e.key.toLowerCase();
      if (k === " " || k === "k") clock.toggle();
      else if (k === "j") clock.slower();
      else if (k === "l") clock.faster();
      else if (k === "arrowright") clock.seek(clock.get().time + (e.shiftKey ? 10 : 1));
      else if (k === "arrowleft") clock.seek(clock.get().time - (e.shiftKey ? 10 : 1));
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clock]);

  return (
    <div className="replay">
      <div className="replay-stage">
        <section className="card replay-scene" aria-label="3D view of both flights">
          <TwoFlightScene sides={sides} zero={zero} clock={clock} camera={camera} />
          <div className="scene-bar">
            <div className="segmented" role="group" aria-label="Camera">
              {([["orbit", "Orbit"], ["top", "Top"], ["chaseA", "Chase A"], ["chaseB", "Chase B"]] as const).map(([m, label]) => (
                <button key={m} aria-pressed={camera === m} onClick={() => setCamera(m)}>{label}</button>
              ))}
            </div>
          </div>
        </section>
        <aside className="replay-side">
          <section className="card card-body"><SideBySide sides={sides} zero={zero} time={time} /></section>
        </aside>
      </div>
      <section className="card card-body replay-controls" aria-label="Playback">
        <div className="controls-row">
          <button className="btn primary play" onClick={() => clock.toggle()} aria-label={playing ? "Pause" : "Play"}>{playing ? "❚❚" : "▶"}</button>
          <select value={rate} onChange={(e) => clock.setRate(Number(e.target.value))} aria-label="Playback speed">
            {RATES.map((r) => <option key={r} value={r}>{r}×</option>)}
          </select>
          <span className="num time-readout">{time < 0 ? "−" : "+"}{fmtTenths(Math.abs(time))} <span className="faint">aligned</span></span>
        </div>
        <input className="compare-scrub" type="range" min={start} max={end} step={0.1} value={time}
          onChange={(e) => clock.seek(Number(e.target.value))} aria-label="Aligned time" />
      </section>
      <CompareCharts sides={sides} zero={zero} time={time} clock={clock} range={[start, end]} />
    </div>
  );
}

function SideBySide({ sides, zero, time }: { sides: [Side, Side]; zero: [number, number]; time: number }) {
  const rows = sides.map((s, i) => {
    const t = time + zero[i];
    const st = stateAt(s.replay, t);
    const e = st.q && st.attAge < 1 ? eulerOf(st.q) : null;
    const { meta, cols } = s.replay;
    const inLog = t >= meta.start && t <= meta.end;
    const mode = heldAt(meta, cols.mode_index, t);
    return {
      inLog,
      height: valueAt(meta, cols.up, t),
      speed: valueAt(meta, cols.ground_speed, t),
      roll: e?.roll, pitch: e?.pitch, yaw: e ? (e.yaw + 360) % 360 : undefined,
      mode: mode >= 0 ? meta.modes[mode] : undefined,
    };
  });
  const cell = (x: number | undefined, d = 1) => (x == null || !Number.isFinite(x) ? "—" : x.toFixed(d));
  return (
    <table className="data side-by-side">
      <thead><tr><th /><th className="r">A</th><th className="r">B</th></tr></thead>
      <tbody>
        <tr><td>Height (m)</td>{rows.map((r, i) => <td key={i} className="r num">{r.inLog ? cell(r.height) : "—"}</td>)}</tr>
        <tr><td>Ground speed (m/s)</td>{rows.map((r, i) => <td key={i} className="r num">{r.inLog ? cell(r.speed) : "—"}</td>)}</tr>
        <tr><td>Roll (°)</td>{rows.map((r, i) => <td key={i} className="r num">{r.inLog ? cell(r.roll, 0) : "—"}</td>)}</tr>
        <tr><td>Pitch (°)</td>{rows.map((r, i) => <td key={i} className="r num">{r.inLog ? cell(r.pitch, 0) : "—"}</td>)}</tr>
        <tr><td>Heading (°)</td>{rows.map((r, i) => <td key={i} className="r num">{r.inLog ? cell(r.yaw, 0) : "—"}</td>)}</tr>
        <tr><td>Mode</td>{rows.map((r, i) => <td key={i} className="r">{r.inLog ? r.mode ?? "—" : <span className="faint">not flying yet / ended</span>}</td>)}</tr>
      </tbody>
    </table>
  );
}

const COMPARE_SIGNALS = [
  { title: "Roll", name: "att.roll" },
  { title: "Height", name: "pos.rel_alt" },
  { title: "Battery", name: "bat.volt" },
];

function CompareCharts({ sides, zero, time, clock, range }: {
  sides: [Side, Side]; zero: [number, number]; time: number; clock: Clock; range: [number, number];
}) {
  const [data, setData] = useState<SeriesData[][] | null>(null);
  useEffect(() => {
    Promise.all(sides.map((s) => api.signals(s.flight.id, COMPARE_SIGNALS.map((c) => c.name), undefined, 2000)))
      .then((r) => setData(r.map((x) => x.signals)), () => setData([]));
  }, [sides]);
  if (!data) return null;
  return (
    <section className="card card-body" aria-label="Charts">
      <div className="chart-grid">
        {COMPARE_SIGNALS.map((c) => {
          const series: Series[] = data
            .map((sigs, i) => ({ s: sigs.find((x) => x.name === c.name), i }))
            .filter(({ s }) => s && !s.missing && s.t)
            .map(({ s, i }) => ({
              name: `${i ? "B" : "A"} ${s!.name}`, unit: s!.unit, t: s!.t!.map((t) => t - zero[i]), v: s!.v!,
              color: `var(${COLORS[i]})`, dash: i === 1,
            }));
          if (!series.length) return null;
          return (
            <div key={c.title}>
              <div className="eyebrow">{c.title}</div>
              <Chart series={series} height={150} marker={time} onPick={(t) => clock.seek(t)} domain={range} syncKey="compare-replay" />
            </div>
          );
        })}
      </div>
    </section>
  );
}

function TwoFlightScene({ sides, zero, clock, camera }: {
  sides: [Side, Side]; zero: [number, number]; clock: Clock; camera: "top" | "orbit" | "chaseA" | "chaseB";
}) {
  const host = useRef<HTMLDivElement>(null);
  const cameraRef = useRef(camera);
  const redraw = useRef<() => void>(() => {});
  const theme = useThemeVersion();
  const [noWebGL, setNoWebGL] = useState(false);
  cameraRef.current = camera;

  useEffect(() => {
    const el = host.current!;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true });
    } catch {
      setNoWebGL(true);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.background = color("--chart-surface", "#fcfcfb");
    scene.add(new THREE.HemisphereLight(0xffffff, 0x888888, 1.4));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(30, 80, 20);
    scene.add(sun);

    // grid over both paths
    let lo = [0, 0], hi = [0, 0], top = 0;
    for (const s of sides) {
      const x = s.replay.meta.extent;
      if (x.east) { lo[0] = Math.min(lo[0], x.east[0]); hi[0] = Math.max(hi[0], x.east[1]); }
      if (x.north) { lo[1] = Math.min(lo[1], x.north[0]); hi[1] = Math.max(hi[1], x.north[1]); }
      if (x.up) top = Math.max(top, x.up[1]);
    }
    const span = Math.max(hi[0] - lo[0], hi[1] - lo[1], 20);
    const step = niceStep(span / 8);
    const size = Math.ceil((span * 1.6) / step) * step;
    const cx = (lo[0] + hi[0]) / 2, cn = (lo[1] + hi[1]) / 2;
    const grid = new THREE.GridHelper(size, Math.round(size / step), color("--line-strong", "#ccc"), color("--line", "#ddd"));
    grid.position.set(Math.round(cx / step) * step, 0, -Math.round(cn / step) * step);
    scene.add(grid);
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.6, 0.9, 32), new THREE.MeshBasicMaterial({ color: color("--text", "#111"), side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.02;
    scene.add(ring);

    const flights = sides.map((s, i) => {
      const { meta, cols } = s.replay;
      const tint = color(COLORS[i], i ? "#eb6834" : "#2a78d6");
      const pts: THREE.Vector3[] = [];
      for (let k = 0; k < meta.rows; k += Math.max(1, Math.floor(meta.rows / 4000))) {
        if (cols.valid_pos[k] > 0) pts.push(new THREE.Vector3(...enuToWorld(cols.east[k], cols.north[k], Number.isFinite(cols.up[k]) ? cols.up[k] : 0)));
      }
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      const line = i === 0
        ? new THREE.Line(geo, new THREE.LineBasicMaterial({ color: tint }))
        : new THREE.Line(geo, new THREE.LineDashedMaterial({ color: tint, dashSize: Math.max(0.6, span / 60), gapSize: Math.max(0.4, span / 90) }));
      line.computeLineDistances();
      scene.add(line);
      const model = buildAircraft(meta.vehicle_type, tint, color("--text-2", "#555"));
      const holder = new THREE.Group();
      holder.add(model);
      scene.add(holder);
      return { side: s, holder, model };
    });

    const cam = new THREE.PerspectiveCamera(55, 1, 0.05, 20000);
    const topCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 20000);
    topCam.up.set(0, 0, -1);
    const controls = new OrbitControls(cam, renderer.domElement);
    cam.position.set(cx - span * 1.1, top + span * 0.8, -cn + span * 1.1);
    controls.target.set(cx, top / 2, -cn);
    controls.update();
    const m4 = new THREE.Matrix4();

    const draw = () => {
      const tau = clock.get().time;
      const pos: THREE.Vector3[] = [];
      const yaw: number[] = [];
      flights.forEach((f, i) => {
        const t = tau + zero[i];
        const { meta } = f.side.replay;
        const inLog = t >= meta.start && t <= meta.end;
        f.holder.visible = inLog;
        const s = stateAt(f.side.replay, Math.min(Math.max(t, meta.start), meta.end));
        const p = new THREE.Vector3(...enuToWorld(Number.isFinite(s.east) ? s.east : 0, Number.isFinite(s.north) ? s.north : 0, Number.isFinite(s.up) ? s.up : 0));
        const q = s.q ?? [1, 0, 0, 0];
        const m = bodyToWorld(q);
        m4.set(m[0], m[1], m[2], 0, m[3], m[4], m[5], 0, m[6], m[7], m[8], 0, 0, 0, 0, 1);
        f.holder.position.copy(p);
        f.holder.quaternion.setFromRotationMatrix(m4);
        pos.push(p);
        yaw.push((eulerOf(q).yaw * Math.PI) / 180);
      });
      const mode = cameraRef.current;
      controls.enabled = mode === "orbit";
      let view: THREE.Camera = cam;
      if (mode === "top") {
        const half = (span * 1.3) / 2, aspect = cam.aspect;
        const [w, h] = aspect >= 1 ? [half * aspect, half] : [half, half / aspect];
        Object.assign(topCam, { left: -w, right: w, top: h, bottom: -h });
        topCam.updateProjectionMatrix();
        topCam.position.set(grid.position.x, top + 100, grid.position.z);
        topCam.lookAt(grid.position.x, 0, grid.position.z);
        view = topCam;
      } else if (mode === "chaseA" || mode === "chaseB") {
        const k = mode === "chaseA" ? 0 : 1;
        const fwd = new THREE.Vector3(Math.sin(yaw[k]), 0, -Math.cos(yaw[k]));
        cam.position.copy(pos[k]).addScaledVector(fwd, -7).add(new THREE.Vector3(0, 3, 0));
        cam.lookAt(pos[k]);
      }
      const ref = mode === "top" ? span * 1.3 : cam.position.distanceTo(pos[0]);
      flights.forEach((f) => f.model.scale.setScalar(Math.max(1, ref / 18)));
      renderer.render(scene, view);
    };
    let pending = 0;
    const request = () => {
      if (!pending) pending = requestAnimationFrame(() => { pending = 0; draw(); });
    };
    redraw.current = request;
    const resize = () => {
      renderer.setSize(el.clientWidth, el.clientHeight, false);
      cam.aspect = el.clientWidth / Math.max(el.clientHeight, 1);
      cam.updateProjectionMatrix();
      request();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(el);
    resize();
    const off = clock.subscribe(request);
    controls.addEventListener("change", request);
    renderer.domElement.addEventListener("webglcontextrestored", request);
    return () => {
      off();
      ro.disconnect();
      if (pending) cancelAnimationFrame(pending);
      controls.dispose();
      scene.traverse((o) => {
        const mesh = o as THREE.Mesh;
        mesh.geometry?.dispose();
        const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
        (Array.isArray(mat) ? mat : mat ? [mat] : []).forEach((x) => x.dispose());
      });
      renderer.dispose();
      el.removeChild(renderer.domElement);
    };
  }, [sides, zero, clock, theme]);

  useEffect(() => redraw.current(), [camera]);
  return (
    <div ref={host} className="scene3d">
      {noWebGL && <div className="empty scene-fallback" role="status">The 3D view needs WebGL, which this browser cannot use. The readouts and charts still work.</div>}
    </div>
  );
}
