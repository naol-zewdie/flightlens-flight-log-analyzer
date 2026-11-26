import * as THREE from "three";
import { describe, expect, it } from "vitest";
import { buildAircraft, spinProps, type Prop } from "./aircraft";

const accent = new THREE.Color("#2a78d6");
const body = new THREE.Color("#555555");

/** World-space centre of every mesh whose material has this emissive colour. */
function lights(model: THREE.Object3D, hex: number): THREE.Vector3[] {
  model.updateMatrixWorld(true);
  const out: THREE.Vector3[] = [];
  model.traverse((o) => {
    const m = (o as THREE.Mesh).material as THREE.MeshStandardMaterial | undefined;
    if (m?.emissive && m.emissive.getHex() === hex) out.push(o.getWorldPosition(new THREE.Vector3()));
  });
  return out;
}

const RED = 0xe0453a, GREEN = 0x2fb35a;

describe("aircraft models (body axes: x forward, y right, z down)", () => {
  it("plane: red light on the left wingtip, green on the right, tips raised", () => {
    const plane = buildAircraft("Fixed wing", accent, body);
    const [red] = lights(plane, RED), [green] = lights(plane, GREEN);
    expect(red.y).toBeLessThan(-0.9); // left
    expect(green.y).toBeGreaterThan(0.9); // right
    expect(red.z).toBeLessThan(-0.04); // dihedral: tips up (−z)
    expect(green.z).toBeLessThan(-0.04);
    const box = new THREE.Box3().setFromObject(plane);
    expect(box.max.x).toBeGreaterThan(0.85); // the nose and propeller lead along +x
    expect(Math.abs(box.min.x)).toBeLessThan(box.max.x + 0.1);
  });

  it("multirotor: the accent-coloured arms are at the front", () => {
    const quad = buildAircraft("Quadrotor", accent, body);
    quad.updateMatrixWorld(true);
    const front: number[] = [];
    quad.traverse((o) => {
      const m = (o as THREE.Mesh).material as THREE.MeshStandardMaterial | undefined;
      const g = (o as THREE.Mesh).geometry;
      if (m?.color?.equals(accent) && g instanceof THREE.CylinderGeometry) front.push(o.getWorldPosition(new THREE.Vector3()).x);
    });
    expect(front).toHaveLength(2);
    for (const x of front) expect(x).toBeGreaterThan(0);
    expect((quad.userData.props as Prop[]).length).toBe(4);
  });

  it("propellers turn only with power, neighbours in opposite directions", () => {
    const hexa = buildAircraft("Hexarotor", accent, body);
    const props = hexa.userData.props as Prop[];
    expect(props).toHaveLength(6);
    spinProps(hexa, 0, 0.5);
    expect(props.every((p) => p.blades.rotation.z === 0)).toBe(true);
    spinProps(hexa, 1, 0.1);
    expect(Math.sign(props[0].blades.rotation.z)).toBe(-Math.sign(props[1].blades.rotation.z));
    expect((props[0].disc.material as THREE.MeshBasicMaterial).opacity).toBeGreaterThan(0);
  });
});
