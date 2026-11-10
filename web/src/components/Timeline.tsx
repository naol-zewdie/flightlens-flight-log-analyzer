import { useRef } from "react";
import type { Finding, FlightEvent } from "../api";
import { fmtTime } from "../format";

interface Props {
  duration: number;
  events: FlightEvent[];
  findings: Finding[];
  cursor: number | null;
  onPick: (t: number) => void;
}

const TICK: Partial<Record<FlightEvent["kind"], string>> = {
  failsafe: "var(--problem)",
  error: "var(--attention)",
  arm: "var(--text-2)",
  disarm: "var(--text-2)",
};

/** Mode bands, event ticks and finding windows on one strip. Click or drag
 *  to move the shared time cursor; arrow keys step 1 s (Shift: 10 s). */
export function Timeline({ duration, events, findings, cursor, onPick }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const D = Math.max(duration, 1);
  const pct = (t: number) => `${Math.min(100, Math.max(0, (t / D) * 100))}%`;

  const modes = events.filter((e) => e.kind === "mode");
  const bands = modes.map((e, i) => ({
    start: e.t,
    end: i + 1 < modes.length ? modes[i + 1].t : D,
    label: e.text,
  }));

  const timeAt = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect();
    return Math.min(D, Math.max(0, ((clientX - r.left) / r.width) * D));
  };

  return (
    <div
      ref={ref}
      className="timeline"
      role="slider"
      tabIndex={0}
      aria-label="Flight timeline"
      aria-valuemin={0}
      aria-valuemax={Math.round(D)}
      aria-valuenow={Math.round(cursor ?? 0)}
      aria-valuetext={fmtTime(cursor ?? 0)}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        onPick(timeAt(e.clientX));
      }}
      onPointerMove={(e) => {
        if (e.buttons === 1) onPick(timeAt(e.clientX));
      }}
      onKeyDown={(e) => {
        const step = e.shiftKey ? 10 : 1;
        if (e.key === "ArrowRight") onPick(Math.min(D, (cursor ?? 0) + step));
        else if (e.key === "ArrowLeft") onPick(Math.max(0, (cursor ?? 0) - step));
        else return;
        e.preventDefault();
      }}
    >
      {bands.map((b) => (
        <div
          key={`${b.start}-${b.label}`}
          className="band"
          style={{ left: pct(b.start), width: `calc(${pct(b.end)} - ${pct(b.start)})` }}
          title={`${b.label} · ${fmtTime(b.start)}–${fmtTime(b.end)}`}
        >
          {b.label}
        </div>
      ))}
      {findings
        .filter((f) => f.start != null && (f.severity === "problem" || f.severity === "attention"))
        .map((f, i) => (
          <div
            key={`w${i}`}
            className="win"
            title={`${f.title} · ${fmtTime(f.start)}`}
            style={{
              left: pct(f.start!),
              width: `max(3px, calc(${pct(f.end ?? f.start!)} - ${pct(f.start!)}))`,
              background: f.severity === "problem" ? "var(--problem)" : "var(--attention)",
            }}
          />
        ))}
      {events
        .filter((e) => TICK[e.kind])
        .map((e, i) => (
          <div key={`e${i}`} className="tick" title={`${fmtTime(e.t)} · ${e.text}`} style={{ left: pct(e.t), background: TICK[e.kind] }} />
        ))}
      {cursor != null && <div className="cursor" style={{ left: pct(cursor) }} />}
      <div className="axis num">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <span key={f}>{fmtTime(f * D)}</span>
        ))}
      </div>
    </div>
  );
}
