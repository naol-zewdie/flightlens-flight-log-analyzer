// Grouping of signals into subsystems and charts, shared by the signal explorer and comparison.
import type { SignalInfo } from "./api";

const SUBSYSTEMS: [string, string][] = [
  ["att", "Attitude"],
  ["rate", "Angular rate"],
  ["vibe", "Vibration"],
  ["mot", "Motors"],
  ["pusher", "Motors"],
  ["ctrl", "Control"],
  ["bat", "Battery"],
  ["power", "Power"],
  ["gps", "GPS"],
  ["pos", "Position"],
  ["ekf", "Estimator"],
  ["rc", "Radio"],
  ["state", "Flight state"],
];

export const groupOf = (name: string) => SUBSYSTEMS.find(([p]) => name.split(".")[0] === p)?.[1] ?? "Other";

export const DEFAULT_SIGNALS = ["att.roll", "att.des_roll", "vibe.metric", "vibe.z", "bat.volt", "mot.1", "mot.2", "mot.3", "mot.4"];

/** Charts by subsystem and unit, at most `perChart` series each (one y axis per chart). */
export function chartGroups(names: string[], catalog: Map<string, SignalInfo>, perChart = 4): string[][] {
  const by = new Map<string, string[]>();
  for (const n of names) {
    const key = `${groupOf(n)}|${catalog.get(n)?.unit ?? ""}`;
    by.set(key, [...(by.get(key) ?? []), n]);
  }
  const out: string[][] = [];
  for (const list of by.values()) for (let i = 0; i < list.length; i += perChart) out.push(list.slice(i, i + perChart));
  return out;
}

/** Signals grouped by subsystem for the picker, optionally filtered by a search string. */
export function pickerGroups(signals: SignalInfo[], filter: string): Map<string, SignalInfo[]> {
  const g = new Map<string, SignalInfo[]>();
  for (const s of signals) {
    if (filter && !s.name.includes(filter.toLowerCase())) continue;
    const k = groupOf(s.name);
    g.set(k, [...(g.get(k) ?? []), s]);
  }
  return g;
}
