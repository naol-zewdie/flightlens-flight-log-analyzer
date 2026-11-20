import { useEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";
import { api, type Spectrum } from "../api";
import { SevLabel } from "../components/Severity";
import { cssVar, useThemeVersion } from "../components/theme";
import { fmtNum, fmtTime } from "../format";
import { useFlight } from "./FlightPage";
import { SERIES_COLORS } from "./Overview";

const AXES = ["x", "y", "z"] as const;
const AXIS_NAME = { x: "Roll (x)", y: "Pitch (y)", z: "Yaw (z)" };

export function SpectrumView() {
  const { flight, findings, cursor, setCursor } = useFlight();
  const [spec, setSpec] = useState<Spectrum | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const finding = findings.find((f) => f.check_id === "vibration.spectrum");

  useEffect(() => {
    let alive = true;
    setSpec(undefined);
    setError(null);
    api
      .spectrum(flight.id)
      .then((r) => alive && setSpec(r.spectrum))
      .catch((e: Error) => alive && setError(e.message));
    return () => {
      alive = false;
    };
  }, [flight.id]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      {finding && (
        <section className="card" aria-label="Noise finding">
          <div className="card-body detail">
            <div>
              <SevLabel severity={finding.severity} />
              <h2 style={{ fontSize: 18, marginTop: 6 }}>{finding.title}</h2>
            </div>
            <p className="lede" style={{ margin: 0 }}>{finding.explanation}</p>
            {finding.recommendation && (
              <div className="advice">
                <div className="eyebrow" style={{ marginBottom: 2 }}>
                  {finding.severity === "not_evaluated" ? "How to get this data" : "What to change"}
                </div>
                {finding.recommendation}
              </div>
            )}
          </div>
        </section>
      )}
      {error ? (
        <div className="card empty">Could not load the spectrum: {error}</div>
      ) : spec === undefined ? (
        <div className="card empty">Loading spectrum…</div>
      ) : spec === null ? (
        !finding && <div className="card empty">No spectrum for this flight.</div>
      ) : (
        <>
          <section className="card" aria-label="Noise spectrum">
            <div className="card-head">
              <h2>Gyro noise by frequency</h2>
              <span className="hint">
                {spec.source} · {fmtNum(spec.fs)} Hz · {fmtNum(spec.air_s)} s in the air
              </span>
            </div>
            <div className="card-body">
              <PsdChart spec={spec} />
            </div>
          </section>
          <section className="card" aria-label="Spectrogram">
            <div className="card-head">
              <h2>Noise over time</h2>
              <span className="hint">all axes · stronger colour is louder · click to move the time cursor</span>
            </div>
            <div className="card-body">
              <Spectrogram spec={spec} cursor={cursor} onPick={setCursor} />
            </div>
          </section>
          {spec.tracking.length > 0 && (
            <section className="card table-wrap" aria-label="Peak against throttle" style={{ maxWidth: 560 }}>
              <div className="card-head">
                <h2>Peak against throttle</h2>
                <span className="hint">
                  {spec.tracks_throttle === true
                    ? "rises with throttle: motor or propeller noise"
                    : spec.tracks_throttle === false
                      ? "stays put: likely a frame resonance"
                      : "throttle varied too little to tell motor noise from resonance"}
                </span>
              </div>
              <table className="data" style={{ marginTop: 8 }}>
                <thead>
                  <tr>
                    <th>Throttle</th>
                    <th>Peak</th>
                  </tr>
                </thead>
                <tbody>
                  {spec.tracking.map(([thr, hz], i) => (
                    <tr key={i}>
                      <td className="num">{Math.round(thr * 100)}%</td>
                      <td className="num">{fmtNum(hz)} Hz</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function PsdChart({ spec }: { spec: Spectrum }) {
  const box = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  const [readout, setReadout] = useState<{ f: number; vals: number[] } | null>(null);
  const top = spec.freqs.filter((f) => f <= spec.fs * 0.475).length;

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const text2 = cssVar("--text-2");
    const text3 = cssVar("--text-3");
    const grid = cssVar("--line");
    const font = `11px ${cssVar("--font") || "sans-serif"}`;
    const marks: { f: number; label: string }[] = [];
    if (spec.peak_hz != null) marks.push({ f: spec.peak_hz, label: `peak ${fmtNum(spec.peak_hz)} Hz` });
    if (spec.hover_hz != null && Math.abs(spec.hover_hz - (spec.peak_hz ?? 0)) > 3)
      marks.push({ f: spec.hover_hz, label: `hover ${fmtNum(spec.hover_hz)} Hz` });
    spec.harmonics.forEach((h, i) => marks.push({ f: h, label: `${i + 2}×` }));

    const data: uPlot.AlignedData = [
      spec.freqs.slice(0, top),
      ...AXES.map((a) => spec.psd_db[a].slice(0, top)),
    ];
    const u = new uPlot(
      {
        width: el.clientWidth,
        height: 260,
        padding: [16, 8, 0, 0],
        legend: { show: false },
        cursor: { drag: { x: true, y: false }, points: { size: 7, width: 2 } },
        scales: { x: { time: false } },
        axes: [
          {
            stroke: text3,
            font,
            grid: { stroke: grid, width: 1 },
            ticks: { stroke: grid, width: 1, size: 4 },
            values: (_u, v) => v.map((x) => `${fmtNum(x)} Hz`),
            space: 70,
          },
          {
            stroke: text3,
            font,
            grid: { stroke: grid, width: 1 },
            ticks: { show: false },
            values: (_u, v) => v.map((x) => `${fmtNum(x)} dB`),
            size: 58,
          },
        ],
        series: [
          {},
          ...AXES.map((a, i) => ({
            label: AXIS_NAME[a],
            stroke: cssVar(SERIES_COLORS[i].slice(4, -1)),
            width: 2,
            points: { show: false },
          })),
        ],
        hooks: {
          draw: [
            (p) => {
              const ctx = p.ctx;
              const dpr = devicePixelRatio || 1;
              const { top: y0, height: h } = p.bbox;
              ctx.save();
              ctx.font = `${11 * dpr}px ${cssVar("--font")}`;
              ctx.fillStyle = text2;
              ctx.strokeStyle = cssVar("--line-strong") || text3;
              ctx.setLineDash([4 * dpr, 4 * dpr]);
              ctx.lineWidth = dpr;
              // labels that would overlap the previous one drop a line
              let lastX = -Infinity;
              let row = 0;
              for (const m of [...marks].sort((a, b) => a.f - b.f)) {
                const x = p.valToPos(m.f, "x", true);
                if (x < p.bbox.left || x > p.bbox.left + p.bbox.width) continue;
                row = x - lastX < 90 * dpr ? row + 1 : 0;
                lastX = x;
                ctx.beginPath();
                ctx.moveTo(x, y0);
                ctx.lineTo(x, y0 + h);
                ctx.stroke();
                ctx.textAlign = "left";
                ctx.fillText(m.label, x + 4 * dpr, y0 + (10 + row * 13) * dpr);
              }
              ctx.restore();
            },
          ],
          setCursor: [
            (p) => {
              const idx = p.cursor.idx;
              if (idx == null) return setReadout(null);
              setReadout({ f: p.data[0][idx], vals: AXES.map((_, i) => p.data[i + 1][idx] as number) });
            },
          ],
        },
      },
      data,
      el,
    );
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: 260 }));
    ro.observe(el);
    const reset = () => u.setScale("x", { min: data[0][0], max: data[0][data[0].length - 1] });
    el.addEventListener("dblclick", reset);
    return () => {
      ro.disconnect();
      el.removeEventListener("dblclick", reset);
      u.destroy();
    };
  }, [spec, theme, top]);

  return (
    <div>
      <div className="chart-legend" aria-live="polite">
        {AXES.map((a, i) => (
          <span key={a}>
            <i style={{ background: SERIES_COLORS[i] }} />
            {AXIS_NAME[a]}
            {readout && <b>{fmtNum(readout.vals[i])} dB</b>}
          </span>
        ))}
        <span className="faint" style={{ marginLeft: "auto" }}>
          {readout ? <span className="num">{fmtNum(readout.f)} Hz</span> : "power spectral density, dB re 1 (deg/s)²/Hz"}
        </span>
      </div>
      <div ref={box} className="chart" role="img" aria-label="Gyro noise spectrum per axis" />
      <div className="hint" style={{ marginTop: 6 }}>drag to zoom, double-click to reset</div>
    </div>
  );
}

const PAD = { left: 58, right: 8, top: 6, bottom: 22 };

function Spectrogram({ spec, cursor, onPick }: { spec: Spectrum; cursor: number | null; onPick: (t: number) => void }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  const [width, setWidth] = useState(800);
  const [hover, setHover] = useState<{ t: number; f: number; db: number | null } | null>(null);
  const height = 220;
  const { t, f, db } = spec.spectrogram;

  // Colour scale: median (the noise floor, left blank) to 99.5th percentile, so the
  // peaks stand out and one loud spike does not wash out the rest.
  const range = useMemo(() => {
    const all = db.flat().filter((v): v is number => v != null).sort((a, b) => a - b);
    if (!all.length) return [0, 1] as const;
    return [all[Math.floor(all.length * 0.5)], all[Math.floor(all.length * 0.995)]] as const;
  }, [db]);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const t0 = t[0] ?? 0;
  const t1 = t[t.length - 1] ?? 1;
  const fMax = f[f.length - 1] ?? 1;
  const plotW = Math.max(10, width - PAD.left - PAD.right);
  const plotH = height - PAD.top - PAD.bottom;
  const xOf = (tv: number) => PAD.left + ((tv - t0) / Math.max(t1 - t0, 1e-9)) * plotW;

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const dpr = devicePixelRatio || 1;
    c.width = width * dpr;
    c.height = height * dpr;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = cssVar("--surface-2");
    ctx.fillRect(PAD.left, PAD.top, plotW, plotH);
    const accent = cssVar("--accent");
    const cw = plotW / Math.max(t.length, 1);
    const ch = plotH / Math.max(f.length, 1);
    ctx.fillStyle = accent;
    for (let i = 0; i < t.length; i++) {
      const row = db[i];
      for (let j = 0; j < f.length; j++) {
        const v = row[j];
        if (v == null) continue;
        const a = Math.min(1, Math.max(0, (v - range[0]) / (range[1] - range[0] || 1)));
        if (a < 0.02) continue;
        ctx.globalAlpha = a;
        ctx.fillRect(PAD.left + i * cw, PAD.top + plotH - (j + 1) * ch, Math.ceil(cw) + 0.5, Math.ceil(ch) + 0.5);
      }
    }
    ctx.globalAlpha = 1;
    // axes
    ctx.fillStyle = cssVar("--text-3");
    ctx.font = `11px ${cssVar("--font") || "sans-serif"}`;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const fStep = niceStep(fMax / 4);
    for (let v = 0; v <= fMax; v += fStep) {
      const y = PAD.top + plotH - (v / fMax) * plotH;
      ctx.fillText(`${fmtNum(v)} Hz`, PAD.left - 6, y);
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const tStep = niceStep((t1 - t0) / Math.max(2, Math.floor(plotW / 110)));
    for (let v = Math.ceil(t0 / tStep) * tStep; v <= t1; v += tStep) {
      ctx.fillText(fmtTime(v), xOf(v), PAD.top + plotH + 6);
    }
    if (cursor != null && cursor >= t0 && cursor <= t1) {
      ctx.strokeStyle = cssVar("--text");
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(xOf(cursor), PAD.top);
      ctx.lineTo(xOf(cursor), PAD.top + plotH);
      ctx.stroke();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, width, theme, range, cursor]);

  const locate = (e: React.MouseEvent) => {
    const r = (e.target as HTMLElement).getBoundingClientRect();
    const x = e.clientX - r.left - PAD.left;
    const y = e.clientY - r.top - PAD.top;
    if (x < 0 || x > plotW || y < 0 || y > plotH) return null;
    const i = Math.min(t.length - 1, Math.floor((x / plotW) * t.length));
    const j = Math.min(f.length - 1, Math.floor(((plotH - y) / plotH) * f.length));
    return { t: t0 + (x / plotW) * (t1 - t0), f: f[j], db: db[i]?.[j] ?? null };
  };

  return (
    <div ref={wrap} style={{ position: "relative" }}>
      <div className="chart-legend">
        <span className="faint" style={{ marginLeft: "auto" }}>
          {hover ? (
            <span className="num">
              {fmtTime(hover.t)} · {fmtNum(hover.f)} Hz · {hover.db == null ? "no data" : `${fmtNum(hover.db)} dB`}
            </span>
          ) : (
            <span className="num">
              {fmtNum(range[0])} to {fmtNum(range[1])} dB
            </span>
          )}
        </span>
      </div>
      <canvas
        ref={canvas}
        style={{ width, height, display: "block", cursor: "crosshair" }}
        role="img"
        aria-label="Spectrogram: gyro noise by frequency over time"
        onMouseMove={(e) => setHover(locate(e))}
        onMouseLeave={() => setHover(null)}
        onClick={(e) => {
          const p = locate(e);
          if (p) onPick(p.t);
        }}
      />
    </div>
  );
}

function niceStep(raw: number): number {
  const mag = 10 ** Math.floor(Math.log10(Math.max(raw, 1e-9)));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * mag;
}
