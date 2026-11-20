import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import { api, type AxisResponse, type Tuning } from "../api";
import { SevLabel } from "../components/Severity";
import { cssVar, useThemeVersion } from "../components/theme";
import { fmtNum } from "../format";
import { useFlight } from "./FlightPage";

const AXES = ["roll", "pitch", "yaw"] as const;
// Same gates as the control.tuning check's defaults: below them the estimate is shown, not judged.
const MIN_WINDOWS = 20;
const MIN_COHERENCE = 0.6;

export function TuningView() {
  const { flight, findings } = useFlight();
  const [tuning, setTuning] = useState<Tuning | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const own = findings.filter((f) => f.check_id === "control.tuning");
  const issues = own.filter((f) => f.severity !== "ok");

  useEffect(() => {
    let alive = true;
    setTuning(undefined);
    setError(null);
    api
      .tuning(flight.id)
      .then((r) => alive && setTuning(r.tuning))
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [flight.id]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      {(issues.length ? issues : own.slice(0, 1)).map((f, i) => (
        <section key={i} className="card" aria-label="Tuning finding">
          <div className="card-body detail">
            <div>
              <SevLabel severity={f.severity} />
              <h2 style={{ fontSize: 18, marginTop: 6 }}>{f.title}</h2>
            </div>
            <p className="lede" style={{ margin: 0 }}>{f.explanation}</p>
            {f.recommendation && (
              <div className="advice">
                <div className="eyebrow" style={{ marginBottom: 2 }}>What to change</div>
                {f.recommendation}
              </div>
            )}
          </div>
        </section>
      ))}
      {error ? (
        <div className="card empty">Could not load the step response: {error}</div>
      ) : tuning === undefined ? (
        <div className="card empty">Loading step response…</div>
      ) : tuning === null ? (
        <div className="card empty">
          No step response for this flight: it needs the rate setpoint and measured rate in the air, with some stick
          inputs or corrections.
        </div>
      ) : (
        <section className="card" aria-label="Step response">
          <div className="card-head">
            <h2>Rate-loop step response</h2>
            <span className="hint">
              estimated from the flight · 1.0 = the commanded rate · shaded: middle half of the windows
            </span>
          </div>
          <div className="card-body tuning-grid">
            {AXES.map((a) => {
              const r = tuning.axes[a];
              return r ? (
                <AxisCard key={a} r={r} />
              ) : (
                <div key={a} className="tuning-axis">
                  <div className="trend-head"><span>{cap(a)}</span></div>
                  <div className="empty" style={{ padding: "32px 0" }}>Too little stick or correction activity.</div>
                </div>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function AxisCard({ r }: { r: AxisResponse }) {
  const reliable = r.windows >= MIN_WINDOWS && r.coherence >= MIN_COHERENCE;
  return (
    <div className="tuning-axis">
      <div className="trend-head">
        <span>{cap(r.axis)}</span>
        <span className={`chip ${reliable ? "" : "stale"}`} title={`${r.windows} windows · coherence ${r.coherence.toFixed(2)}`}>
          {reliable ? (r.axis === "yaw" ? "shown, not judged" : "reliable") : "indicative only"}
        </span>
      </div>
      <StepChart r={r} />
      <div className="kv" style={{ marginTop: 6 }}>
        <span><span>Peak</span><span className="num">{r.peak.toFixed(2)}×</span></span>
        <span><span>Settles</span><span className="num">{r.final.toFixed(2)}×</span></span>
        <span><span>Rise</span><span className="num">{r.rise_s != null ? `${Math.round(r.rise_s * 1000)} ms` : "—"}</span></span>
        <span><span>Windows</span><span className="num">{r.windows}</span></span>
        <span><span>Coherence</span><span className="num">{r.coherence.toFixed(2)}</span></span>
      </div>
    </div>
  );
}

function StepChart({ r }: { r: AxisResponse }) {
  const box = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const text3 = cssVar("--text-3");
    const grid = cssVar("--line");
    const accent = cssVar("--series-1");
    const font = `10px ${cssVar("--font") || "sans-serif"}`;
    const ms = r.t.map((t) => t * 1000);
    const top = Math.max(1.4, ...r.high) * 1.05;
    const u = new uPlot(
      {
        width: el.clientWidth,
        height: 170,
        padding: [8, 8, 0, 0],
        legend: { show: false },
        cursor: { drag: { x: false, y: false }, points: { size: 6 } },
        scales: { x: { time: false }, y: { range: [Math.min(0, ...r.low) * 1.05, top] } },
        axes: [
          { stroke: text3, font, grid: { stroke: grid, width: 1 }, ticks: { show: false },
            values: (_u, v) => v.map((x) => `${fmtNum(x)} ms`), space: 50, size: 24 },
          { stroke: text3, font, grid: { stroke: grid, width: 1 }, ticks: { show: false },
            values: (_u, v) => v.map((x) => fmtNum(x)), size: 34, space: 24 },
        ],
        series: [
          {},
          { stroke: "transparent", points: { show: false } },
          { stroke: "transparent", points: { show: false } },
          { stroke: accent, width: 2, points: { show: false } },
        ],
        bands: [{ series: [2, 1], fill: cssVar("--accent-tint") || "rgba(42,120,214,0.12)" }],
        hooks: {
          draw: [
            (p) => {
              // the commanded rate: a perfect response settles on this line
              const ctx = p.ctx;
              const dpr = devicePixelRatio || 1;
              const y = p.valToPos(1, "y", true);
              ctx.save();
              ctx.strokeStyle = text3;
              ctx.setLineDash([4 * dpr, 4 * dpr]);
              ctx.lineWidth = dpr;
              ctx.beginPath();
              ctx.moveTo(p.bbox.left, y);
              ctx.lineTo(p.bbox.left + p.bbox.width, y);
              ctx.stroke();
              ctx.restore();
            },
          ],
        },
      },
      [ms, r.low, r.high, r.step],
      el,
    );
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: 170 }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.destroy();
    };
  }, [r, theme]);

  return (
    <div
      ref={box}
      role="img"
      aria-label={`${r.axis} rate step response: peak ${r.peak.toFixed(2)}, settles at ${r.final.toFixed(2)} of the commanded rate`}
    />
  );
}
