// The 3D view: ground grid, flight path, finding windows, and the aircraft at
// the clock's time. Everything is built in code (no model files, no textures,
// nothing downloaded) and drawn only when something changes.

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { Finding } from "../api";
import { cssVar, useThemeVersion } from "../components/theme";
import type { Clock } from "./clock";
import { bodyToWorld, enuToWorld, eulerOf, stateAt, type Quat, type Replay } from "./data";

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
}

/** A theme colour as a three.js colour. Translucent tokens (rgba) are blended onto
 *  the chart surface first, since three.js line colours have no alpha. */
function color(v: string, fallback: string): THREE.Color {
  const raw = cssVar(v) || fallback;
  const m = raw.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (!m || m[4] === undefined) return new THREE.Color(raw);
  const a = Number(m[4]);
  const bg = new THREE.Color(cssVar("--chart-surface") || "#fcfcfb");
  const fg = new THREE.Color(Number(m[1]) / 255, Number(m[2]) / 255, Number(m[3]) / 255);
  return bg.lerp(fg, a);
}

/** Simple aircraft models in body axes: x forward, y right, z down (1 unit ≈ 1 m). */
function buildAircraft(vehicle: string | null, accent: THREE.Color, body: THREE.Color): THREE.Group {
  const g = new THREE.Group();
  const mat = new THREE.MeshStandardMaterial({ color: body, roughness: 0.6, metalness: 0.1 });
  const nose = new THREE.MeshStandardMaterial({ color: accent, roughness: 0.4 });
  const box = (x: number, y: number, z: number, m = mat) => new THREE.Mesh(new THREE.BoxGeometry(x, y, z), m);
  const v = (vehicle ?? "").toLowerCase();
  const rotor = (x: number, y: number, r: number) => {
    const disc = new THREE.Mesh(
      new THREE.CylinderGeometry(r, r, 0.02, 24),
      new THREE.MeshStandardMaterial({ color: body, transparent: true, opacity: 0.35 }),
    );
    disc.rotation.x = Math.PI / 2; // cylinder axis along body z (vertical)
    disc.position.set(x, y, -0.08);
    g.add(disc);
  };
  const fixedWing = v.includes("fixed wing") || v.includes("plane") || v.includes("vtol");
  if (fixedWing) {
    g.add(box(1.4, 0.16, 0.16)); // fuselage
    const wing = box(0.3, 2.0, 0.04);
    wing.position.set(0.1, 0, -0.02);
    g.add(wing);
    const tail = box(0.18, 0.7, 0.03);
    tail.position.set(-0.62, 0, -0.02);
    g.add(tail);
    const fin = box(0.2, 0.03, 0.28);
    fin.position.set(-0.62, 0, -0.16);
    g.add(fin);
    const tip = box(0.18, 0.17, 0.17, nose);
    tip.position.set(0.75, 0, 0);
    g.add(tip);
    if (v.includes("vtol")) for (const [x, y] of [[0.45, 0.6], [0.45, -0.6], [-0.35, 0.6], [-0.35, -0.6]]) rotor(x, y, 0.22);
  } else {
    const arms = v.includes("octo") ? 8 : v.includes("hexa") ? 6 : 4;
    const reach = 0.55;
    for (let k = 0; k < arms; k++) {
      const a = (Math.PI * 2 * k) / arms + Math.PI / arms;
      const arm = box(reach, 0.05, 0.04);
      arm.position.set(Math.cos(a) * reach / 2, Math.sin(a) * reach / 2, 0);
      arm.rotation.z = a;
      g.add(arm);
      rotor(Math.cos(a) * reach, Math.sin(a) * reach, 0.2);
    }
    g.add(box(0.32, 0.22, 0.1));
    const tip = box(0.16, 0.1, 0.08, nose); // the front, in the accent colour
    tip.position.set(0.22, 0, 0);
    g.add(tip);
  }
  return g;
}

function niceStep(raw: number): number {
  const p = 10 ** Math.floor(Math.log10(Math.max(raw, 1e-6)));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}

export function Scene3D({ replay, clock, findings, camera }: Props) {
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
    const draw = () => {
      const s = stateAt(replay, clock.get().time);
      const pos = new THREE.Vector3(...enuToWorld(
        Number.isFinite(s.east) ? s.east : 0,
        Number.isFinite(s.north) ? s.north : 0,
        Number.isFinite(s.up) ? s.up : 0,
      ));
      // attitude: logged, or level along the direction of travel when there is none
      let q: Quat | null = s.q;
      if (!q) {
        const k = Math.round((s.t - meta.start) * meta.rate_hz);
        const ve = cols.ve?.[k], vn = cols.vn?.[k];
        const heading = Number.isFinite(ve) && Number.isFinite(vn) && Math.hypot(ve, vn) > 0.5 ? Math.atan2(ve, vn) : 0;
        q = [Math.cos(heading / 2), 0, 0, Math.sin(heading / 2)];
      }
      const m = bodyToWorld(q);
      world.set(m[0], m[1], m[2], 0, m[3], m[4], m[5], 0, m[6], m[7], m[8], 0, 0, 0, 0, 1);
      holder.position.copy(pos);
      holder.quaternion.setFromRotationMatrix(world);

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
      if (mode === "chase") {
        cam.up.set(0, 1, 0);
        cam.position.copy(pos).addScaledVector(fwd, -7).add(tmp.set(0, 3, 0));
        cam.lookAt(pos);
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
  }, [replay, clock, findings, theme]);

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
