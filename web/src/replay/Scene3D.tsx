// The 3D view: ground grid, flight path, finding windows, and the aircraft at
// the clock's time. Everything is built in code (no model files, no textures,
// nothing downloaded) and drawn only when something changes.

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { Finding } from "../api";
import type { Note } from "./notes";
import { cssVar, useThemeVersion } from "../components/theme";
import type { Clock } from "./clock";
import { buildAircraft, spinProps } from "./aircraft";
import { bodyToWorld, enuToWorld, eulerOf, heldAt, stateAt, type Quat, type Replay } from "./data";

export type CameraMode = "chase" | "orbit" | "top" | "cockpit";
export const CAMERAS: { mode: CameraMode; label: string; key: string }[] = [
  { mode: "chase", label: "Chase", key: "1" },
  { mode: "orbit", label: "Orbit", key: "2" },
  { mode: "top", label: "Top", key: "3" },
  { mode: "cockpit", label: "Cockpit", key: "4" },
];

interface Props {
  replay: Replay;
  clock: Clock;
  findings: Finding[];
  camera: CameraMode;
  /** Note markers on the path */
  notes?: Note[];
}

/** A theme colour as a three.js colour. Translucent tokens (rgba) are blended onto
 *  the chart surface first, since three.js line colours have no alpha. */
export function color(v: string, fallback: string): THREE.Color {
  const raw = cssVar(v) || fallback;
  const m = raw.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (!m || m[4] === undefined) return new THREE.Color(raw);
  const a = Number(m[4]);
  const bg = new THREE.Color(cssVar("--chart-surface") || "#fcfcfb");
  const fg = new THREE.Color(Number(m[1]) / 255, Number(m[2]) / 255, Number(m[3]) / 255);
  return bg.lerp(fg, a);
}

export { buildAircraft };

export function niceStep(raw: number): number {
  const p = 10 ** Math.floor(Math.log10(Math.max(raw, 1e-6)));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}

export function Scene3D({ replay, clock, findings, camera, notes = [] }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const cameraMode = useRef(camera);
  const redraw = useRef<() => void>(() => {});
  const theme = useThemeVersion();
  const [noWebGL, setNoWebGL] = useState(false);
  cameraMode.current = camera;

  useEffect(() => {
    const el = host.current!;
    const { meta, cols } = replay;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true });
    } catch {
      // no WebGL (no GPU, remote desktop, disabled by policy): the rest of the
      // replay still works, so say so instead of failing the page
      setNoWebGL(true);
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    el.appendChild(renderer.domElement);
    renderer.domElement.setAttribute("aria-hidden", "true");

    const scene = new THREE.Scene();
    scene.background = color("--chart-surface", "#fcfcfb");
    scene.add(new THREE.HemisphereLight(0xffffff, 0x888888, 1.4));
    const sun = new THREE.DirectionalLight(0xffffff, 1.2);
    sun.position.set(30, 80, 20);
    scene.add(sun);

    // ground grid sized to the path
    const ext = meta.extent;
    const span = Math.max(
      ext.east ? ext.east[1] - ext.east[0] : 0,
      ext.north ? ext.north[1] - ext.north[0] : 0,
      20,
    );
    const step = niceStep(span / 8);
    const size = Math.ceil((span * 1.6) / step) * step;
    const cx = ext.east ? (ext.east[0] + ext.east[1]) / 2 : 0;
    const cn = ext.north ? (ext.north[0] + ext.north[1]) / 2 : 0;
    const grid = new THREE.GridHelper(size, Math.round(size / step), color("--line-strong", "#ccc"), color("--line", "#ddd"));
    grid.position.set(Math.round(cx / step) * step, 0, -Math.round(cn / step) * step);
    scene.add(grid);
    // a faint ground to the horizon, so the horizon shows in every view
    const groundColor = color("--chart-surface", "#fcfcfb").lerp(color("--text-3", "#888"), 0.14);
    const ground = new THREE.Mesh(
      new THREE.CircleGeometry(Math.max(size * 40, 5000), 64),
      new THREE.MeshBasicMaterial({ color: groundColor }),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.set(grid.position.x, -0.05, grid.position.z);
    scene.add(ground);

    // north arrow on the ground, and the take-off point
    const northMat = new THREE.LineBasicMaterial({ color: color("--text-3", "#888") });
    const arrowLen = step;
    const north = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, 0.01, 0), new THREE.Vector3(0, 0.01, -arrowLen),
      new THREE.Vector3(-arrowLen * 0.15, 0.01, -arrowLen * 0.8), new THREE.Vector3(0, 0.01, -arrowLen),
      new THREE.Vector3(arrowLen * 0.15, 0.01, -arrowLen * 0.8), new THREE.Vector3(0, 0.01, -arrowLen),
    ]);
    const northLine = new THREE.LineSegments(north, northMat);
    northLine.position.set(grid.position.x - size / 2 + step, 0, grid.position.z + size / 2 - step);
    scene.add(northLine);
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.6, 0.9, 32),
      new THREE.MeshBasicMaterial({ color: color("--text", "#111"), side: THREE.DoubleSide }),
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.02;
    scene.add(ring);

    // the path: one line per continuous run of valid position
    const pathMat = new THREE.LineBasicMaterial({ color: color("--text-3", "#999"), transparent: true, opacity: 0.8 });
    const flownMat = new THREE.LineBasicMaterial({ color: color("--accent", "#2a78d6") });
    const runs: { from: number; to: number; geo: THREE.BufferGeometry; flown: THREE.Line }[] = [];
    const hasPos = meta.sources.position != null;
    if (hasPos) {
      let from = -1;
      const close = (to: number) => {
        if (to - from < 1) return;
        const pts = new Float32Array((to - from + 1) * 3);
        for (let k = from; k <= to; k++) {
          const [x, y, z] = enuToWorld(cols.east[k], cols.north[k], Number.isFinite(cols.up[k]) ? cols.up[k] : 0);
          pts.set([x, y, z], (k - from) * 3);
        }
        const geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.BufferAttribute(pts, 3));
        scene.add(new THREE.Line(geo, pathMat));
        const flown = new THREE.Line(geo, flownMat);
        flown.geometry = geo.clone();
        scene.add(flown);
        runs.push({ from, to, geo, flown });
      };
      for (let k = 0; k < meta.rows; k++) {
        const ok = cols.valid_pos[k] > 0;
        if (ok && from < 0) from = k;
        if (!ok && from >= 0) {
          close(k - 1);
          from = -1;
        }
      }
      if (from >= 0) close(meta.rows - 1);
      // finding windows: the path during each problem or attention window, coloured
      for (const f of findings) {
        if (f.start == null || (f.severity !== "problem" && f.severity !== "attention")) continue;
        const a = Math.max(0, Math.floor((f.start - meta.start) * meta.rate_hz));
        const b = Math.min(meta.rows - 1, Math.ceil(((f.end ?? f.start) - meta.start) * meta.rate_hz));
        const pts: THREE.Vector3[] = [];
        for (let k = a; k <= b; k++) {
          if (cols.valid_pos[k] > 0) pts.push(new THREE.Vector3(...enuToWorld(cols.east[k], cols.north[k], Number.isFinite(cols.up[k]) ? cols.up[k] : 0)));
        }
        if (pts.length < 2) continue;
        const m = new THREE.LineBasicMaterial({ color: color(f.severity === "problem" ? "--problem" : "--attention", "#d03b3b"), linewidth: 2 });
        scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), m));
      }
    }

    // note markers: a small diamond at each note's moment on the path
    if (hasPos) {
      const noteMat = new THREE.MeshBasicMaterial({ color: color("--series-4", "#6250d6") });
      for (const n of notes) {
        const k = Math.round((n.t_start - meta.start) * meta.rate_hz);
        if (k < 0 || k >= meta.rows || !(cols.valid_pos[k] > 0)) continue;
        const marker = new THREE.Mesh(new THREE.OctahedronGeometry(Math.max(0.5, span / 120)), noteMat);
        const up = Number.isFinite(cols.up[k]) ? cols.up[k] : 0;
        marker.position.set(...enuToWorld(cols.east[k], cols.north[k], up + Math.max(1, span / 60)));
        scene.add(marker);
      }
    }

    // the aircraft and its drop line to the ground
    const aircraft = buildAircraft(meta.vehicle_type, color("--accent", "#2a78d6"), color("--text-2", "#555"));
    const holder = new THREE.Group(); // world placement; the model inside is in body axes
    holder.add(aircraft);
    scene.add(holder);
    const dropGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3()]);
    const drop = new THREE.Line(dropGeo, new THREE.LineDashedMaterial({ color: color("--text-3", "#999"), dashSize: 0.5, gapSize: 0.4 }));
    scene.add(drop);

    const cam = new THREE.PerspectiveCamera(55, 1, 0.05, 20000);
    // the top view is straight down without perspective, like the map
    const topCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 20000);
    topCam.up.set(0, 0, -1); // north up
    cam.position.set(-10, 8, 10);
    const controls = new OrbitControls(cam, renderer.domElement);
    controls.enableDamping = false;
    let orbitOffset = new THREE.Vector3(-12, 8, 12);
    let lastMode: CameraMode | null = null;

    const world = new THREE.Matrix4();
    const tmp = new THREE.Vector3();
    // the first logged attitude, kept level: where the aircraft faces before it is logged
    const firstAttitude = ((): Quat => {
      const k = cols.valid_att ? cols.valid_att.findIndex((v) => v > 0) : -1;
      if (k < 0) return [1, 0, 0, 0];
      const yaw = (eulerOf([cols.qw[k], cols.qx[k], cols.qy[k], cols.qz[k]]).yaw * Math.PI) / 180;
      return [Math.cos(yaw / 2), 0, 0, Math.sin(yaw / 2)];
    })();
    // throttle as 0-1 (some logs record percent)
    const thrustScale = (() => {
      let max = 0;
      for (const v of cols.thrust ?? []) if (Number.isFinite(v) && v > max) max = v;
      return max > 1.5 ? 100 : 1;
    })();
    // the chase camera swings round behind the aircraft like a chase plane instead
    // of being bolted to its heading
    const chase = { pos: new THREE.Vector3(), ready: false };
    const CHASE_TAU = 0.6; // s, how quickly it swings round
    let lastTime = NaN, lastWall = 0;
    const draw = () => {
      const now = performance.now();
      const wallDt = lastWall ? Math.min(0.1, (now - lastWall) / 1000) : 0;
      lastWall = now;
      const { time, playing } = clock.get();
      const replayDt = Number.isFinite(lastTime) ? time - lastTime : 0;
      lastTime = time;
      const s = stateAt(replay, time);
      const pos = new THREE.Vector3(...enuToWorld(
        Number.isFinite(s.east) ? s.east : 0,
        Number.isFinite(s.north) ? s.north : 0,
        Number.isFinite(s.up) ? s.up : 0,
      ));
      // attitude: logged (held through gaps), or, before the first attitude sample,
      // level along the direction of travel, or facing where it will first point
      let q: Quat | null = s.q;
      if (!q) {
        const k = Math.round((s.t - meta.start) * meta.rate_hz);
        const ve = cols.ve?.[k], vn = cols.vn?.[k];
        if (Number.isFinite(ve) && Number.isFinite(vn) && Math.hypot(ve, vn) > 0.5) {
          const heading = Math.atan2(ve, vn);
          q = [Math.cos(heading / 2), 0, 0, Math.sin(heading / 2)];
        } else {
          q = firstAttitude;
        }
      }
      const m = bodyToWorld(q);
      world.set(m[0], m[1], m[2], 0, m[3], m[4], m[5], 0, m[6], m[7], m[8], 0, 0, 0, 0, 1);
      holder.position.copy(pos);
      holder.quaternion.setFromRotationMatrix(world);
      // propellers turn while armed, faster with more throttle
      const armed = heldAt(meta, cols.armed, time) > 0;
      const thr = heldAt(meta, cols.thrust, time) / thrustScale;
      const power = armed ? (Number.isFinite(thr) ? Math.max(0.15, thr) : 0.5) : 0;
      if (playing && replayDt > 0 && replayDt < 1) spinProps(aircraft, power, replayDt);
      else if (!armed) spinProps(aircraft, 0, 0);

      // the flown part of each run
      const idx = Math.round((s.t - meta.start) * meta.rate_hz);
      for (const r of runs) {
        const n = Math.max(0, Math.min(r.to, idx) - r.from + 1);
        r.flown.geometry.setDrawRange(0, n);
      }
      const p = dropGeo.attributes.position as THREE.BufferAttribute;
      p.setXYZ(0, pos.x, pos.y, pos.z);
      p.setXYZ(1, pos.x, 0, pos.z);
      p.needsUpdate = true;
      drop.computeLineDistances();

      const mode = cameraMode.current;
      const e = eulerOf(q);
      const yaw = (e.yaw * Math.PI) / 180;
      const fwd = new THREE.Vector3(Math.sin(yaw), 0, -Math.cos(yaw)); // horizontal heading
      controls.enabled = mode === "orbit";
      holder.visible = mode !== "cockpit";
      let settling = false;
      if (mode === "chase") {
        cam.up.set(0, 1, 0);
        // the camera's offset from the aircraft eases round behind it (no lag in
        // distance, however fast it flies); a seek or a mode change snaps
        const want = tmp.copy(fwd).multiplyScalar(-7).add(new THREE.Vector3(0, 3, 0));
        const jumped = !chase.ready || lastMode !== "chase" || Math.abs(replayDt) > 1;
        if (jumped || wallDt === 0) chase.pos.copy(want);
        else chase.pos.lerp(want, 1 - Math.exp(-wallDt / CHASE_TAU)).setLength(want.length());
        chase.ready = true;
        cam.position.copy(pos).add(chase.pos);
        cam.lookAt(pos);
        // paused or scrubbing: keep drawing until the camera has swung round
        settling = chase.pos.distanceTo(want) > 0.02;
      } else if (mode === "orbit") {
        if (lastMode !== "orbit") {
          cam.up.set(0, 1, 0);
          cam.position.copy(pos).add(orbitOffset);
        } else {
          orbitOffset = cam.position.clone().sub(controls.target);
          cam.position.copy(pos).add(orbitOffset);
        }
        controls.target.copy(pos);
        controls.update();
      } else if (mode === "top") {
        const half = (span * 1.3) / 2;
        const aspect = cam.aspect;
        const [w, h] = aspect >= 1 ? [half * aspect, half] : [half, half / aspect];
        Object.assign(topCam, { left: -w, right: w, top: h, bottom: -h });
        topCam.updateProjectionMatrix();
        const high = (ext.up ? Math.max(ext.up[1], 0) : 0) + 100;
        topCam.position.set(grid.position.x, high, grid.position.z);
        topCam.lookAt(grid.position.x, 0, grid.position.z);
      } else {
        // cockpit: the camera looks along the body's x axis, its up is body −z
        const fwdB = new THREE.Vector3(m[0], m[3], m[6]);
        const upB = new THREE.Vector3(-m[2], -m[5], -m[8]);
        cam.up.copy(upB);
        cam.position.copy(pos).addScaledVector(fwdB, 0.3);
        cam.lookAt(tmp.copy(cam.position).add(fwdB));
      }
      lastMode = mode;
      // keep the model visible from far away (top view of a long flight)
      const dist = mode === "top" ? span * 1.3 : cam.position.distanceTo(pos);
      aircraft.scale.setScalar(Math.max(1, dist / 18));
      renderer.render(scene, mode === "top" ? topCam : cam);
      if (settling && !playing) request();
    };

    let pending = 0;
    const request = () => {
      if (!pending) pending = requestAnimationFrame(() => {
        pending = 0;
        draw();
      });
    };
    redraw.current = request;
    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h, false);
      cam.aspect = w / Math.max(h, 1);
      cam.updateProjectionMatrix();
      request();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(el);
    resize();
    const off = clock.subscribe(request);
    // a GPU reset loses the WebGL context; three.js restores it, then we must draw again
    const restored = () => request();
    renderer.domElement.addEventListener("webglcontextrestored", restored);
    controls.addEventListener("change", () => {
      if (cameraMode.current === "orbit") request();
    });

    return () => {
      off();
      renderer.domElement.removeEventListener("webglcontextrestored", restored);
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
  }, [replay, clock, findings, theme, notes]);

  useEffect(() => redraw.current(), [camera]);

  return (
    <div ref={host} className="scene3d">
      {noWebGL && (
        <div className="empty scene-fallback" role="status">
          The 3D view needs WebGL, which this browser has turned off or cannot use (no
          graphics acceleration). The instruments, map, charts and timeline still work.
        </div>
      )}
    </div>
  );
}
