import { useMemo } from "react";
import type { Track } from "../api";
import { fmtNum, fmtTime } from "../format";

const R = 6371000;

interface Props {
  track: Track;
  marker: number | null;
  window?: { start: number; end: number; color: string } | null;
  onPick?: (t: number) => void;
}

/** Flight path in local metres (east/north of the take-off point) on a scale
 *  grid. Works fully offline; north is up and both axes share one scale. */
export function TrackView({ track, marker, window: win, onPick }: Props) {
  const geo = useMemo(() => {
    const n = track.lat.length;
    if (n < 2) return null;
    const lat0 = track.lat[0];
    const lon0 = track.lon[0];
    const k = Math.PI / 180;
    const cos0 = Math.cos(lat0 * k);
    const xs = track.lon.map((lo) => (lo - lon0) * k * R * cos0);
    const ys = track.lat.map((la) => (la - lat0) * k * R);
    let minX = Math.min(...xs), maxX = Math.max(...xs);
    let minY = Math.min(...ys), maxY = Math.max(...ys);
    // Fit the path to the 2:1 view box with one shared scale and a 12% margin.
    const dx = Math.max(maxX - minX, 10), dy = Math.max(maxY - minY, 10);
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const half = Math.max(dx / 2, dy) * 1.12;
    minX = cx - half; maxX = cx + half;
    minY = cy - half / 2; maxY = cy + half / 2;
    const span = Math.max(dx, dy);
    const step = niceStep(span / 5);
    return { xs, ys, minX, maxX, minY, maxY, step, span };
  }, [track]);

  if (!geo) {
    return <div className="empty">No position data in this log, so there is no flight path to draw.</div>;
  }
  const W = 600, H = 300;
  const s = Math.min(W / (geo.maxX - geo.minX), H / (geo.maxY - geo.minY));
  const ox = (W - (geo.maxX - geo.minX) * s) / 2;
  const oy = (H - (geo.maxY - geo.minY) * s) / 2;
  const px = (x: number) => ox + (x - geo.minX) * s;
  const py = (y: number) => H - (oy + (y - geo.minY) * s);

  const pts = geo.xs.map((x, i) => `${px(x).toFixed(1)},${py(geo.ys[i]).toFixed(1)}`);
  const idxAt = (t: number) => {
    let lo = 0, hi = track.t.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (track.t[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  };
  const winPts =
    win != null
      ? pts.slice(idxAt(win.start), Math.max(idxAt(win.end), idxAt(win.start) + 1) + 1)
      : [];
  const m = marker != null ? idxAt(marker) : null;

  const gridX: number[] = [];
  for (let g = Math.ceil(geo.minX / geo.step) * geo.step; g <= geo.maxX; g += geo.step) gridX.push(g);
  const gridY: number[] = [];
  for (let g = Math.ceil(geo.minY / geo.step) * geo.step; g <= geo.maxY; g += geo.step) gridY.push(g);

  const pick = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!onPick) return;
    const r = e.currentTarget.getBoundingClientRect();
    const scale = Math.max(W / r.width, H / r.height);
    const cx = (e.clientX - r.left) * scale - (r.width * scale - W) / 2;
    const cy = (e.clientY - r.top) * scale - (r.height * scale - H) / 2;
    let best = 0, bestD = Infinity;
    geo.xs.forEach((x, i) => {
      const d = (px(x) - cx) ** 2 + (py(geo.ys[i]) - cy) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    });
    onPick(track.t[best]);
  };

  return (
    <div className="track">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" onClick={pick} role="img"
        aria-label="Flight path seen from above, in metres from the take-off point">
        <rect x={0} y={0} width={W} height={H} fill="var(--chart-surface)" />
        {gridX.map((g) => (
          <line key={`x${g}`} x1={px(g)} x2={px(g)} y1={0} y2={H} stroke="var(--line)" />
        ))}
        {gridY.map((g) => (
          <line key={`y${g}`} y1={py(g)} y2={py(g)} x1={0} x2={W} stroke="var(--line)" />
        ))}
        <polyline points={pts.join(" ")} fill="none" stroke="var(--text-2)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" opacity={0.75} />
        {winPts.length > 1 && (
          <polyline points={winPts.join(" ")} fill="none" stroke={win!.color} strokeWidth={4} strokeLinejoin="round" strokeLinecap="round" />
        )}
        <circle cx={px(geo.xs[0])} cy={py(geo.ys[0])} r={5} fill="var(--chart-surface)" stroke="var(--text)" strokeWidth={2} />
        <rect x={px(geo.xs[geo.xs.length - 1]) - 4} y={py(geo.ys[geo.ys.length - 1]) - 4} width={8} height={8} fill="var(--text)" />
        {m != null && (
          <g>
            <circle cx={px(geo.xs[m])} cy={py(geo.ys[m])} r={9} fill="var(--accent)" opacity={0.2} />
            <circle cx={px(geo.xs[m])} cy={py(geo.ys[m])} r={5} fill="var(--accent)" stroke="var(--chart-surface)" strokeWidth={2} />
          </g>
        )}
        <g fontSize={11} fill="var(--text-3)" fontFamily="var(--font)">
          <text x={10} y={H - 10}>grid {fmtNum(geo.step)} m</text>
          <text x={W - 10} y={18} textAnchor="end">N ↑</text>
        </g>
      </svg>
      <div className="chart-legend" style={{ paddingTop: 8 }}>
        <span>○ take-off</span>
        <span>■ end</span>
        {m != null && (
          <span>
            <i style={{ background: "var(--accent)", height: 8, width: 8, borderRadius: 4 }} />
            <span className="num">{fmtTime(track.t[m])}</span>
            {track.alt[m] != null && <span className="num"> · {fmtNum(track.alt[m])} m above take-off</span>}
          </span>
        )}
        <span className="faint" style={{ marginLeft: "auto" }}>{track.source}</span>
      </div>
    </div>
  );
}

function niceStep(raw: number): number {
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}
