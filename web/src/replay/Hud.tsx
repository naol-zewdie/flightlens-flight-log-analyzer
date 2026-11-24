// Flight instruments at the clock's time, drawn in SVG with the theme's tokens.
// A value the log does not have shows as "—", never as 0.

import { fmtNum } from "../format";
import { useClock, type Clock } from "./clock";
import { eulerOf, heldAt, stateAt, valueAt, type Replay } from "./data";

const FIX: Record<number, string> = { 0: "no GPS", 1: "no fix", 2: "2D", 3: "3D", 4: "DGPS", 5: "RTK float", 6: "RTK fixed" };

const num = (x: number, digits = 1) => (Number.isFinite(x) ? x.toFixed(digits) : "—");

export function Hud({ replay, clock }: { replay: Replay; clock: Clock }) {
  const time = useClock(clock, (s) => s.time);
  const { meta, cols } = replay;
  const s = stateAt(replay, time);
  const att = s.q && s.attAge < 1 ? eulerOf(s.q) : null;
  const v = (name: string) => valueAt(meta, cols[name], time);
  const mode = heldAt(meta, cols.mode_index, time);
  const armed = heldAt(meta, cols.armed, time);
  const fix = heldAt(meta, cols.gps_fix, time);
  const sats = heldAt(meta, cols.gps_sats, time);
  const remaining = v("bat_remaining");
  const stale = Math.min(s.posAge, s.attAge);

  return (
    <div className="hud" aria-label="Flight instruments">
      <Horizon roll={att?.roll ?? null} pitch={att?.pitch ?? null} />
      <div className="hud-heading num" title="Heading (true north)">
        {att ? `${String(Math.round((att.yaw + 360) % 360)).padStart(3, "0")}°` : "—"}
        <span className="faint"> heading</span>
      </div>
      <dl className="hud-grid">
        <Item k="Height" v={num(v("up"))} u="m above take-off" />
        <Item k="Altitude" v={num(v("alt_amsl"), 0)} u="m above sea level" />
        <Item k="Ground speed" v={num(v("ground_speed"))} u="m/s" />
        <Item k="Airspeed" v={num(v("airspeed"))} u="m/s" hide={!meta.columns.includes("airspeed") || !anyFinite(cols.airspeed)} />
        <Item k="Climb" v={num(v("climb"))} u="m/s" />
        <Item k="Mode" v={mode >= 0 && meta.modes[mode] ? meta.modes[mode] : "—"} text />
        <Item k="Armed" v={Number.isFinite(armed) ? (armed > 0 ? "Armed" : "Disarmed") : "—"} text />
        <Item k="Battery" v={num(v("bat_volt"), 2)} u={Number.isFinite(remaining) ? `V · ${fmtNum(remaining * 100)}%` : "V"} />
        <Item k="GPS" v={Number.isFinite(fix) ? FIX[fix] ?? `fix ${fix}` : "—"} u={Number.isFinite(sats) ? `${sats} sats` : undefined} text />
      </dl>
      <div className="hud-foot faint">
        {meta.rate_hz} Hz replay
        {stale > 0.5 && Number.isFinite(stale) && <span className="hud-gap"> · no data for {fmtNum(stale)} s</span>}
      </div>
    </div>
  );
}

function anyFinite(col: Float32Array | undefined): boolean {
  if (!col) return false;
  for (let i = 0; i < col.length; i += 25) if (Number.isFinite(col[i])) return true;
  return false;
}

function Item({ k, v, u, text, hide }: { k: string; v: string; u?: string; text?: boolean; hide?: boolean }) {
  if (hide) return null;
  return (
    <div>
      <dt>{k}</dt>
      <dd className={text ? "" : "num"}>
        {v}
        {u && v !== "—" && <small> {u}</small>}
      </dd>
    </div>
  );
}

/** Artificial horizon: the sky/ground line tilts with roll and moves with pitch. */
function Horizon({ roll, pitch }: { roll: number | null; pitch: number | null }) {
  const R = 54;
  const pxPerDeg = 1.6;
  const known = roll != null && pitch != null;
  const r = roll ?? 0, p = Math.max(-60, Math.min(60, pitch ?? 0));
  return (
    <svg className="horizon" viewBox="-64 -64 128 128" role="img"
      aria-label={known ? `Roll ${Math.round(r)} degrees, pitch ${Math.round(p)} degrees` : "No attitude data"}>
      <defs>
        <clipPath id="hz-clip"><circle r={R} /></clipPath>
      </defs>
      <g clipPath="url(#hz-clip)">
        <g transform={`rotate(${-r}) translate(0 ${p * pxPerDeg})`} opacity={known ? 1 : 0.35}>
          <rect x={-200} y={-400} width={400} height={400} fill="var(--hud-sky)" />
          <rect x={-200} y={0} width={400} height={400} fill="var(--hud-ground)" />
          <line x1={-200} x2={200} y1={0} y2={0} stroke="var(--surface)" strokeWidth={1.5} />
          {[-30, -20, -10, 10, 20, 30].map((d) => (
            <g key={d}>
              <line x1={d % 20 === 0 ? -14 : -8} x2={d % 20 === 0 ? 14 : 8} y1={-d * pxPerDeg} y2={-d * pxPerDeg}
                stroke="var(--surface)" strokeWidth={1} opacity={0.85} />
              {d % 20 === 0 && (
                <text x={18} y={-d * pxPerDeg + 3} fontSize={7} fill="var(--surface)">{Math.abs(d)}</text>
              )}
            </g>
          ))}
        </g>
      </g>
      <circle r={R} fill="none" stroke="var(--line-strong)" />
      {/* roll scale and pointer */}
      {[-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60].map((d) => {
        const a = ((d - 90) * Math.PI) / 180;
        const inner = d % 30 === 0 ? R - 7 : R - 4;
        return <line key={d} x1={Math.cos(a) * inner} y1={Math.sin(a) * inner} x2={Math.cos(a) * R} y2={Math.sin(a) * R} stroke="var(--text-2)" strokeWidth={1} />;
      })}
      <g transform={`rotate(${-r})`}>
        <path d={`M 0 ${-R + 8} l -4 7 h 8 z`} fill="var(--accent)" />
      </g>
      {/* the aircraft symbol, fixed */}
      <path d="M -30 0 h 18 l 6 6 l 6 -6 h 18" fill="none" stroke="var(--attention)" strokeWidth={2.5} strokeLinejoin="round" />
      <circle r={1.8} fill="var(--attention)" />
      {!known && <text y={4} textAnchor="middle" fontSize={9} fill="var(--text-2)">no attitude</text>}
      <text y={60} textAnchor="middle" fontSize={8} fill="var(--text-2)" className="num">
        {known ? `roll ${Math.round(r)}° · pitch ${Math.round(pitch!)}°` : ""}
      </text>
    </svg>
  );
}
