// The 3D aircraft models, in body axes: x forward, y right, z down (the log's frame).
// Built from three.js shapes, so nothing is downloaded and the replay stays offline.
// Real aircraft cues make the direction readable at a glance: navigation lights (red
// on the left wingtip, green on the right, white at the tail) and, on a multirotor,
// the front arms in the accent colour.

import * as THREE from "three";

/** A propeller: spun about its own axis while the aircraft is armed. */
export interface Prop {
  blades: THREE.Object3D;
  disc: THREE.Mesh;
  /** +1 or −1: neighbouring multirotor props turn opposite ways */
  dir: number;
}

const RED = 0xe0453a;
const GREEN = 0x2fb35a;
const WHITE = 0xf4f4f0;

function mat(c: THREE.ColorRepresentation, opts: THREE.MeshStandardMaterialParameters = {}) {
  return new THREE.MeshStandardMaterial({ color: c, roughness: 0.55, metalness: 0.15, ...opts });
}

function light(c: number, r = 0.035): THREE.Mesh {
  return new THREE.Mesh(
    new THREE.SphereGeometry(r, 12, 8),
    new THREE.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: 0.9 }),
  );
}

/** A lifting surface in the body x-y plane: root at y=0, tip at y=span. */
function panel(root: number, tip: number, span: number, sweep: number, thick: number, m: THREE.Material): THREE.Mesh {
  const s = new THREE.Shape();
  s.moveTo(root / 2, 0);
  s.lineTo(tip / 2 - sweep, span);
  s.lineTo(-tip / 2 - sweep, span);
  s.lineTo(-root / 2, 0);
  s.closePath();
  const g = new THREE.ExtrudeGeometry(s, { depth: thick, bevelEnabled: true, bevelThickness: thick / 2, bevelSize: thick / 2, bevelSegments: 2 });
  g.translate(0, 0, -thick / 2);
  return new THREE.Mesh(g, m);
}

function propeller(radius: number, m: THREE.Material, dir: number): Prop & { group: THREE.Group } {
  // in a group whose local z is the shaft; blades and disc lie in its x-y plane
  const group = new THREE.Group();
  const blades = new THREE.Group();
  for (const a of [0, Math.PI]) {
    const blade = new THREE.Mesh(new THREE.BoxGeometry(radius, radius * 0.14, radius * 0.03), m);
    blade.position.set(Math.cos(a) * radius / 2, Math.sin(a) * radius / 2, 0);
    blade.rotation.z = a;
    blade.rotation.x = 0.25 * dir; // pitch
    blades.add(blade);
  }
  group.add(blades);
  const disc = new THREE.Mesh(
    new THREE.CircleGeometry(radius, 32),
    new THREE.MeshBasicMaterial({ color: 0x999999, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false }),
  );
  group.add(disc);
  return { group, blades, disc, dir };
}

function fixedWing(accent: THREE.Color, body: THREE.Color, vtol: boolean): { g: THREE.Group; props: Prop[] } {
  const g = new THREE.Group();
  const skin = mat(body.clone().lerp(new THREE.Color(WHITE), 0.55));
  const trim = mat(accent);
  const dark = mat(0x2b2b2b, { roughness: 0.4 });

  // fuselage: a streamlined body of revolution along x (lathe is about y; turn y → x)
  const profile = [
    [0, 0.78], [0.05, 0.74], [0.085, 0.62], [0.1, 0.42], [0.1, 0.15],
    [0.085, -0.2], [0.055, -0.55], [0.03, -0.78], [0, -0.8],
  ].map(([r, y]) => new THREE.Vector2(r, y));
  const fuselage = new THREE.Mesh(new THREE.LatheGeometry(profile, 24), skin);
  fuselage.rotation.z = -Math.PI / 2;
  g.add(fuselage);
  // canopy, nose cone in the accent colour
  const canopy = new THREE.Mesh(new THREE.SphereGeometry(0.075, 16, 12), mat(0x1d2a38, { roughness: 0.15, metalness: 0.4 }));
  canopy.scale.set(2.6, 0.9, 0.8);
  canopy.position.set(0.38, 0, -0.07);
  g.add(canopy);
  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.05, 0.12, 16), trim);
  nose.rotation.z = -Math.PI / 2;
  nose.position.set(0.81, 0, 0);
  g.add(nose);

  // wings: tapered, a little sweep and 4° dihedral (tips up = −z)
  const dihedral = (4 * Math.PI) / 180;
  for (const side of [1, -1]) {
    const w = panel(0.34, 0.18, 1.05, 0.06, 0.03, skin);
    w.scale.y = side;
    w.rotation.x = -side * dihedral; // tips rise toward −z (up) on both sides
    w.position.set(0.12, 0, -0.04);
    g.add(w);
    const tipLight = light(side > 0 ? GREEN : RED);
    tipLight.position.set(0.12 - 0.06, side * 1.05, -0.04 - Math.sin(dihedral) * 1.05);
    g.add(tipLight);
  }
  // tailplane and fin
  for (const side of [1, -1]) {
    const t = panel(0.2, 0.12, 0.36, 0.05, 0.02, skin);
    t.scale.y = side;
    t.position.set(-0.68, 0, -0.03);
    g.add(t);
  }
  const fin = panel(0.24, 0.12, 0.3, 0.1, 0.02, trim);
  fin.rotation.x = -Math.PI / 2; // span along −z (up)
  fin.position.set(-0.66, 0, -0.03);
  g.add(fin);
  const tailLight = light(WHITE, 0.03);
  tailLight.position.set(-0.8, 0, -0.03);
  g.add(tailLight);

  const props: Prop[] = [];
  // tractor propeller at the nose: shaft along x
  const p = propeller(0.2, dark, 1);
  p.group.rotation.y = Math.PI / 2;
  p.group.position.set(0.88, 0, 0);
  g.add(p.group);
  props.push(p);
  if (vtol) {
    for (const [x, y, d] of [[0.5, 0.55, 1], [0.5, -0.55, -1], [-0.3, 0.55, -1], [-0.3, -0.55, 1]]) {
      const boom = new THREE.Mesh(new THREE.CylinderGeometry(0.018, 0.018, 1.0, 8), dark);
      boom.rotation.z = Math.PI / 2;
      boom.position.set(0.1, y, -0.06);
      g.add(boom);
      const q = propeller(0.2, dark, d);
      q.group.position.set(x, y, -0.12);
      g.add(q.group);
      props.push(q);
    }
  }
  return { g, props };
}

function multirotor(arms: number, accent: THREE.Color, body: THREE.Color): { g: THREE.Group; props: Prop[] } {
  const g = new THREE.Group();
  const frame = mat(body.clone().lerp(new THREE.Color(0x222222), 0.45), { roughness: 0.45 });
  const front = mat(accent);
  const motorMat = mat(0x3a3a3a, { metalness: 0.6, roughness: 0.3 });
  const bladeMat = mat(0x1e1e1e, { roughness: 0.35 });
  const reach = 0.55;
  const props: Prop[] = [];

  // centre body: a rounded plate with a canopy on top (up = −z)
  const plate = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.05, 6), frame);
  plate.rotation.x = Math.PI / 2;
  g.add(plate);
  const canopy = new THREE.Mesh(new THREE.SphereGeometry(0.13, 20, 12, 0, Math.PI * 2, 0, Math.PI / 2), mat(body.clone().lerp(new THREE.Color(WHITE), 0.6)));
  canopy.rotation.x = -Math.PI / 2; // dome toward −z
  canopy.scale.set(1.3, 1, 0.8);
  canopy.position.z = -0.02;
  g.add(canopy);
  const camera = new THREE.Mesh(new THREE.SphereGeometry(0.045, 12, 10), mat(0x111111, { roughness: 0.2 }));
  camera.position.set(0.15, 0, 0.05);
  g.add(camera);

  for (let k = 0; k < arms; k++) {
    const a = (Math.PI * 2 * k) / arms + Math.PI / arms; // arms either side of the nose
    const isFront = Math.cos(a) > 0.01;
    const arm = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.025, reach, 10), isFront ? front : frame);
    arm.rotation.z = a - Math.PI / 2; // cylinder axis (y) along the arm
    arm.position.set((Math.cos(a) * reach) / 2, (Math.sin(a) * reach) / 2, 0);
    g.add(arm);
    const [x, y] = [Math.cos(a) * reach, Math.sin(a) * reach];
    const motor = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.045, 0.06, 16), motorMat);
    motor.rotation.x = Math.PI / 2;
    motor.position.set(x, y, -0.03);
    g.add(motor);
    const p = propeller(0.2, bladeMat, k % 2 === 0 ? 1 : -1);
    p.group.position.set(x, y, -0.07);
    g.add(p.group);
    props.push(p);
    // arm-tip lights: red left, green right, white at the back
    const l = light(isFront ? (y < 0 ? RED : GREEN) : WHITE, 0.025);
    l.position.set(x, y, 0.03);
    g.add(l);
  }
  // landing skids
  for (const side of [1, -1]) {
    const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.01, 0.01, 0.14, 6), frame);
    leg.rotation.x = Math.PI / 2;
    leg.position.set(0, side * 0.1, 0.09);
    g.add(leg);
    const skid = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, 0.36, 8), frame);
    skid.rotation.z = Math.PI / 2;
    skid.position.set(0, side * 0.1, 0.16);
    g.add(skid);
  }
  return { g, props };
}

/** The model for a vehicle type; its propellers are in `userData.props`. */
export function buildAircraft(vehicle: string | null, accent: THREE.Color, body: THREE.Color): THREE.Group {
  const v = (vehicle ?? "").toLowerCase();
  const fixed = v.includes("fixed wing") || v.includes("plane") || v.includes("vtol");
  const arms = v.includes("octo") ? 8 : v.includes("hexa") ? 6 : 4;
  const { g, props } = fixed ? fixedWing(accent, body, v.includes("vtol")) : multirotor(arms, accent, body);
  g.userData.props = props;
  return g;
}

/** Spin the propellers: `power` 0 (stopped) to 1 (full), over `dt` seconds of replay. */
export function spinProps(model: THREE.Object3D, power: number, dt: number): void {
  const props = (model.userData.props ?? []) as Prop[];
  const p = Math.max(0, Math.min(1, power));
  for (const prop of props) {
    // turn slowly enough to read as rotation on screen; the blur disc shows speed
    prop.blades.rotation.z += prop.dir * (p > 0 ? 8 + 22 * p : 0) * dt;
    (prop.disc.material as THREE.MeshBasicMaterial).opacity = p > 0 ? 0.12 + 0.25 * p : 0;
  }
}
