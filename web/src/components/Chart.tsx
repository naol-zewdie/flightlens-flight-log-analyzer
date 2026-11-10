import { useEffect, useMemo, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { fmtNum, fmtTime } from "../format";
import { cssVar, useThemeVersion } from "./theme";

export interface Series {
  name: string;
  unit?: string;
  t: number[];
  v: (number | null)[];
  /** CSS colour or custom property, e.g. "var(--series-1)" */
  color: string;
  dash?: boolean;
}

export interface Band {
  start: number;
  end: number;
  label?: string;
}

interface Props {
  series: Series[];
  height?: number;
  /** Shaded problem window */
  window?: { start: number; end: number; color: string } | null;
  /** Horizontal threshold line */
  threshold?: { value: number; label: string; color: string } | null;
  /** Light background bands, e.g. flight modes */
  bands?: Band[];
  /** Externally controlled time marker (seconds) */
  marker?: number | null;
  onHover?: (t: number | null) => void;
  onPick?: (t: number) => void;
  /** Called with the zoomed x range, or null when zoom is reset */
  onZoom?: (range: [number, number] | null) => void;
  syncKey?: string;
  xRange?: [number, number] | null;
  /** Full time span (s). Zoom counts as reset only when it covers this span;
   *  double-click resets to it. Defaults to the loaded data's extent. */
  domain?: [number, number] | null;
}

/** Put every series on one shared, sorted x axis (union of all sample times).
 *  Values between samples are interpolated; explicit gaps (null) stay gaps. */
function align(series: Series[]): uPlot.AlignedData {
  const xs = new Set<number>();
  for (const s of series) for (const t of s.t) xs.add(t);
  const x = Float64Array.from(xs).sort();
  const out: (number | null)[][] = series.map((s) => {
    const ys: (number | null)[] = new Array(x.length).fill(null);
    let j = 0;
    for (let i = 0; i < x.length; i++) {
      const xi = x[i];
      while (j < s.t.length - 1 && s.t[j + 1] <= xi) j++;
      if (s.t.length === 0 || xi < s.t[0] || xi > s.t[s.t.length - 1]) continue;
      if (s.t[j] === xi) {
        ys[i] = s.v[j];
        continue;
      }
      const a = s.v[j];
      const b = s.v[j + 1];
      if (a == null || b == null || j + 1 >= s.t.length) continue;
      const f = (xi - s.t[j]) / (s.t[j + 1] - s.t[j]);
      ys[i] = a + (b - a) * f;
    }
    return ys;
  });
  return [Array.from(x), ...out] as uPlot.AlignedData;
}

function resolveColor(c: string): string {
  const m = c.match(/^var\((--[\w-]+)\)$/);
  return m ? cssVar(m[1]) : c;
}

export function Chart(props: Props) {
  const { series, height = 220, syncKey } = props;
  const box = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const live = useRef(props);
  live.current = props;
  const theme = useThemeVersion();
  const [readout, setReadout] = useState<{ t: number; vals: (number | null)[] } | null>(null);

  const data = useMemo(() => align(series), [series]);
  const units = Array.from(new Set(series.map((s) => s.unit).filter(Boolean)));

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const text2 = cssVar("--text-2");
    const text3 = cssVar("--text-3");
    const grid = cssVar("--line");
    const font = `11px ${cssVar("--font") || "sans-serif"}`;

    const drawExtras = (u: uPlot) => {
      const p = live.current;
      const ctx = u.ctx;
      const { left, top, width, height: h } = u.bbox;
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, top, width, h);
      ctx.clip();
      // flight-mode bands
      (p.bands ?? []).forEach((b, i) => {
        if (i % 2 === 1) return;
        const x0 = u.valToPos(b.start, "x", true);
        const x1 = u.valToPos(b.end, "x", true);
        ctx.fillStyle = cssVar("--neutral-tint");
        ctx.fillRect(x0, top, x1 - x0, h);
      });
      // problem window
      if (p.window) {
        const x0 = u.valToPos(p.window.start, "x", true);
        const x1 = Math.max(u.valToPos(p.window.end, "x", true), x0 + 2);
        ctx.globalAlpha = 0.14;
        ctx.fillStyle = resolveColor(p.window.color);
        ctx.fillRect(x0, top, x1 - x0, h);
        ctx.globalAlpha = 1;
      }
      ctx.restore();
    };

    const drawOver = (u: uPlot) => {
      const p = live.current;
      const ctx = u.ctx;
      const { left, top, width, height: h } = u.bbox;
      const dpr = devicePixelRatio || 1;
      ctx.save();
      if (p.threshold) {
        const y = u.valToPos(p.threshold.value, "y", true);
        if (y >= top && y <= top + h) {
          ctx.strokeStyle = resolveColor(p.threshold.color);
          ctx.lineWidth = 1.5 * dpr;
          ctx.setLineDash([5 * dpr, 4 * dpr]);
          ctx.beginPath();
          ctx.moveTo(left, y);
          ctx.lineTo(left + width, y);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.font = `${11 * dpr}px ${cssVar("--font")}`;
          ctx.fillStyle = text2;
          ctx.textAlign = "right";
          ctx.fillText(p.threshold.label, left + width - 4 * dpr, y - 5 * dpr);
        }
      }
      if (p.marker != null) {
        const x = u.valToPos(p.marker, "x", true);
        if (x >= left && x <= left + width) {
          ctx.strokeStyle = cssVar("--accent");
          ctx.lineWidth = 2 * dpr;
          ctx.beginPath();
          ctx.moveTo(x, top);
          ctx.lineTo(x, top + h);
          ctx.stroke();
        }
      }
      ctx.restore();
    };

    const opts: uPlot.Options = {
      width: el.clientWidth,
      height,
      padding: [8, 8, 0, 0],
      legend: { show: false },
      cursor: {
        sync: syncKey ? { key: syncKey } : undefined,
        // Zoom is applied in the setSelect hook, so only a real drag counts as a zoom.
        drag: { x: true, y: false, setScale: false },
        // Our own double-click handler resets to the full flight (`domain`);
        // uPlot's would reset only to the loaded (possibly zoomed) data.
        bind: { dblclick: () => () => null },
        points: { size: 7, width: 2 },
      },
      scales: { x: { time: false, ...(props.xRange ? { min: props.xRange[0], max: props.xRange[1] } : {}) } },
      axes: [
        {
          stroke: text3,
          font,
          grid: { stroke: grid, width: 1 },
          ticks: { stroke: grid, width: 1, size: 4 },
          values: (_u, vals) => vals.map((v) => fmtTime(v)),
          space: 70,
        },
        {
          stroke: text3,
          font,
          grid: { stroke: grid, width: 1 },
          ticks: { show: false },
          values: (_u, vals) => vals.map((v) => fmtNum(v)),
          size: 48,
        },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.name,
          stroke: resolveColor(s.color),
          width: 2,
          dash: s.dash ? [6, 4] : undefined,
          spanGaps: false,
          points: { show: false },
        })),
      ],
      hooks: {
        drawClear: [drawExtras],
        draw: [drawOver],
        setCursor: [
          (u) => {
            const idx = u.cursor.idx;
            if (idx == null) {
              setReadout(null);
              live.current.onHover?.(null);
              return;
            }
            const t = u.data[0][idx];
            setReadout({ t, vals: u.data.slice(1).map((d) => (d[idx] ?? null) as number | null) });
            live.current.onHover?.(t);
          },
        ],
        // uPlot also changes the scale on its own (creation, new data), so zoom is
        // reported only from the two user actions: a drag here and the double-click below.
        setSelect: [
          (u) => {
            const { left, width } = u.select;
            if (width < 3) return;
            const min = u.posToVal(left, "x");
            const max = u.posToVal(left + width, "x");
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
            u.setScale("x", { min, max });
            live.current.onZoom?.([min, max]);
          },
        ],
      },
    };

    const u = new uPlot(opts, data, el);
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height }));
    ro.observe(el);
    const reset = () => {
      const [lo, hi] = live.current.domain ?? [data[0][0] ?? 0, data[0][data[0].length - 1] ?? 1];
      u.setScale("x", { min: lo, max: hi });
      live.current.onZoom?.(null);
    };
    el.addEventListener("dblclick", reset);
    // A click (not a drag-to-zoom) picks that moment.
    let downX = 0;
    const onDown = (e: MouseEvent) => (downX = e.clientX);
    const onUp = (e: MouseEvent) => {
      if (Math.abs(e.clientX - downX) < 3 && live.current.onPick) {
        live.current.onPick(u.posToVal(e.offsetX, "x"));
      }
    };
    u.over.addEventListener("mousedown", onDown);
    u.over.addEventListener("mouseup", onUp);
    return () => {
      ro.disconnect();
      el.removeEventListener("dblclick", reset);
      u.over.removeEventListener("mousedown", onDown);
      u.over.removeEventListener("mouseup", onUp);
      u.destroy();
      plot.current = null;
    };
    // Rebuild only when the data, layout or theme changes; overlays redraw below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, height, theme, syncKey, series.length]);

  // Overlays changed: redraw without rebuilding.
  useEffect(() => {
    plot.current?.redraw(false);
  }, [props.marker, props.window, props.threshold, props.bands]);

  useEffect(() => {
    const u = plot.current;
    if (u && props.xRange) u.setScale("x", { min: props.xRange[0], max: props.xRange[1] });
  }, [props.xRange]);

  return (
    <div>
      <div className="chart-legend" aria-live="polite">
        {series.map((s, i) => (
          <span key={s.name}>
            <i style={{ background: s.color, ...(s.dash ? { backgroundImage: "none", opacity: 0.8 } : {}) }} />
            {s.name}
            {readout && <b>{fmtNum(readout.vals[i])}</b>}
          </span>
        ))}
        <span className="faint" style={{ marginLeft: "auto" }}>
          {readout ? <span className="num">{fmtTime(readout.t)}</span> : units.join(" · ")}
        </span>
      </div>
      <div ref={box} className="chart" role="img" aria-label={`Chart of ${series.map((s) => s.name).join(", ")}`} />
    </div>
  );
}
