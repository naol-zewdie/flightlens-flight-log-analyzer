import { describe, expect, it } from "vitest";
import type { Finding } from "../api";
import { criticalWindow, incidentFor, incidentSummary } from "./IncidentPanel";

const f = (title: string, start: number | null, extra: Partial<Finding> = {}): Finding => ({
  check_id: "x", severity: "problem", title, explanation: "", recommendation: "", measured: null, threshold: null,
  comparison: ">", unit: "", start, end: start == null ? null : start + 2, evidence_signals: [], key: `${title}@${start}`, ...extra,
});

describe("incident mode", () => {
  const crash = f("Crash", 120, { check_id: "flight.crash", related: ["Vibration too high", "Out of trim", "Battery voltage sagged"] });
  const findings = [crash, f("Battery voltage sagged", 110), f("Vibration too high", 60), f("Out of trim", null)];

  it("lists the likely causes in time order, standing ones last", () => {
    const i = incidentFor("Crash@120", findings)!;
    expect(i.causes.map((c) => c.title)).toEqual(["Vibration too high", "Battery voltage sagged", "Out of trim"]);
    expect(i.causes[2].finding?.start).toBeNull();
    expect(incidentFor("nope", findings)).toBeNull();
    expect(incidentFor(null, findings)).toBeNull();
  });

  it("loops the critical window inside the log", () => {
    expect(criticalWindow(crash, 0, 500)).toEqual([105, 127]);
    expect(criticalWindow(crash, 110, 124)).toEqual([110, 124]);
  });

  it("starts a debrief with the incident and its causes", () => {
    const text = incidentSummary(incidentFor("Crash@120", findings)!);
    expect(text).toContain("Crash at 02:00–02:02.");
    expect(text).toContain("1. Vibration too high (01:00)");
    expect(text).toContain("3. Out of trim (throughout the flight)");
  });
});
