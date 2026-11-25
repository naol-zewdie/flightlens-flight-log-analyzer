// The Replay tab: 3D view, instruments, map, charts and timeline on one clock.
// See docs/replay.md for what it does and why.

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type SeriesData } from "../api";
import { Chart, type Series } from "../components/Chart";
import { useReadOnly } from "../components/server";
import { SevIcon } from "../components/Severity";
import { Timeline } from "../components/Timeline";
import { TrackView } from "../components/TrackView";
import { fmtTime, fmtWindow } from "../format";
import { useFlight } from "../pages/FlightPage";
import { createClock, RATES, throttleTime, useClock, type Clock } from "./clock";
import { eulerOf, loadReplay, stateAt, type Replay } from "./data";
import { Hud } from "./Hud";
import { CAMERAS, Scene3D, type CameraMode } from "./Scene3D";
import { coverage, videoApi, type Video } from "./video";
import { VideoPanel } from "./VideoPanel";
import { notesApi, type Note } from "./notes";
import { NotesPanel } from "./NotesPanel";
import { criticalWindow, IncidentPanel, incidentFor, INCIDENT_LEAD_S, type Incident } from "./IncidentPanel";

/** Seconds before a finding that "Replay from here" and the list jump to. */
export const LEAD_S = 5;
const FOLLOW_WINDOW_S = 60;
const PANELS = ["video", "hud", "map", "charts", "list", "notes"] as const;
type Panel = (typeof PANELS)[number];

function storedPanels(): Record<Panel, boolean> {
  const all = { video: true, hud: true, map: true, charts: true, list: true, notes: true };
  try {
    const raw = localStorage.getItem("fl-replay-panels");
    return raw ? { ...all, ...JSON.parse(raw) } : all;
  } catch {
    return all;
  }
}

export default function ReplayView() {
  const { flight, findings, events, track, cursor, setCursor, reload } = useFlight();
  const readOnly = useReadOnly();
  const [params, setParams] = useSearchParams();
  const [replay, setReplay] = useState<Replay | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ac = new AbortController();
    setReplay(null);
    setError(null);
    loadReplay(flight.id, ac.signal).then(setReplay, (e: Error) => {
      if (e.name !== "AbortError") setError(e.message);
    });
    return () => ac.abort();
  }, [flight.id]);

  if (error) {
    return (
      <div className="card card-body">
        <h2>Replay not available</h2>
        <p className="muted">{error}</p>
        {!readOnly && /re-analyze/i.test(error) && (
          <button className="btn" onClick={() => api.reanalyze(flight.id).then(reload)}>Re-analyze</button>
        )}
      </div>
    );
  }
  if (!replay) return <div className="empty">Loading the replay…</div>;
  const t0 = Number(params.get("t"));
  const incident = incidentFor(params.get("incident"), findings);
  const initial = incident
    ? Math.max(replay.meta.start, incident.finding.start! - INCIDENT_LEAD_S)
    : params.has("t") && Number.isFinite(t0) ? t0 : cursor ?? replay.meta.start;
  const setParam = (name: string, value: string | null) =>
    setParams((p) => {
      const next = new URLSearchParams(p);
      if (value == null) next.delete(name);
      else next.set(name, value);
      return next;
    }, { replace: true });
  return (
    <ReplayScreen
      key={flight.id}
      replay={replay}
      initial={initial}
      findings={findings}
      events={events}
      track={track}
      setCursor={setCursor}
      setTimeParam={(t) => setParam("t", t == null ? null : t.toFixed(1))}
      incident={incident}
      exitIncident={() => setParam("incident", null)}
      flightId={flight.id}
      logStart={flight.start_time}
      readOnly={readOnly}
    />
  );
}

interface ScreenProps {
  replay: Replay;
  initial: number;
  findings: ReturnType<typeof useFlight>["findings"];
  events: ReturnType<typeof useFlight>["events"];
  track: ReturnType<typeof useFlight>["track"];
  setCursor: (t: number | null) => void;
  setTimeParam: (t: number | null) => void;
  incident: Incident | null;
  exitIncident: () => void;
  flightId: string;
  logStart: string | null;
  readOnly: boolean;
}

function ReplayScreen({ replay, initial, findings, events, track, setCursor, setTimeParam, incident, exitIncident, flightId, logStart, readOnly }: ScreenProps) {
  const { meta } = replay;
  const clock = useMemo(() => createClock(meta.start, meta.end, initial), [replay]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => clock.dispose(), [clock]);
  // incident mode loops the critical window around the finding
  const incidentKey = incident?.finding.key;
  useEffect(() => {
    if (incident) clock.setLoop(criticalWindow(incident.finding, meta.start, meta.end));
  }, [clock, incidentKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const [camera, setCamera] = useState<CameraMode>("chase");
  const [follow, setFollow] = useState(true);
  const [panels, setPanels] = useState(storedPanels);
  const playing = useClock(clock, (s) => s.playing);
  const rate = useClock(clock, (s) => s.rate);
  const loop = useClock(clock, (s) => s.loop);
  const [slowTime, setSlowTime] = useState(clock.get().time); // ~10 Hz, for charts and lists
  const [videos, setVideos] = useState<Video[]>([]);
  const [selectedVideo, setSelectedVideo] = useState<string | null>(null);
  useEffect(() => {
    videoApi.list(flightId).then((v) => {
      setVideos(v);
      setSelectedVideo((cur) => cur ?? v[0]?.id ?? null);
    }, () => undefined);
  }, [flightId]);
  const [notes, setNotes] = useState<Note[]>([]);
  const [composeAt, setComposeAt] = useState<{ t: number; end: number | null } | null>(null);
  useEffect(() => {
    notesApi.list(flightId).then(setNotes, () => undefined);
  }, [flightId]);
  const shown = videos.find((v) => v.id === selectedVideo);
  const covered = shown ? coverage(shown) : null;

  // The clock moves the cursor every tab shares, and the URL when paused.
  useEffect(() => throttleTime(clock, 100, (t) => {
    setSlowTime(t);
    setCursor(t);
  }), [clock, setCursor]);
  useEffect(() => {
    if (!playing) setTimeParam(clock.get().time);
  }, [playing, slowTime]); // eslint-disable-line react-hooks/exhaustive-deps

  const togglePanel = (p: Panel) =>
    setPanels((cur) => {
      const next = { ...cur, [p]: !cur[p] };
      try {
        localStorage.setItem("fl-replay-panels", JSON.stringify(next));
      } catch {
        /* storage unavailable */
      }
      return next;
    });

  // keyboard: space/K play, J/L speed, arrows step, , . one sample, Home/End, 1-4 cameras
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.closest("input, textarea, select, [contenteditable=true]") || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = clock.get().time;
      const sample = 1 / meta.rate_hz;
      const k = e.key.toLowerCase();
      if (k === " " || k === "k") clock.toggle();
      else if (k === "j") clock.slower();
      else if (k === "l") clock.faster();
      else if (k === "arrowright" && !el.closest(".timeline")) clock.seek(t + (e.shiftKey ? 10 : 1));
      else if (k === "arrowleft" && !el.closest(".timeline")) clock.seek(t - (e.shiftKey ? 10 : 1));
      else if (k === "." ) clock.seek(t + sample);
      else if (k === ",") clock.seek(t - sample);
      else if (k === "home") clock.seek(meta.start);
      else if (k === "end") clock.seek(meta.end);
      else if (CAMERAS.some((c) => c.key === k)) setCamera(CAMERAS.find((c) => c.key === k)!.mode);
      else if (k === "n" && !readOnly) {
        clock.pause();
        setPanels((cur) => ({ ...cur, notes: true }));
        setComposeAt({ t: t, end: null });
      }
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clock, meta, readOnly]);

  const sceneNotes: string[] = [];
  if (!meta.sources.position) sceneNotes.push("No position in this log: the aircraft turns in place, without a path.");
  if (!meta.sources.attitude) sceneNotes.push("No attitude in this log: the model is level, pointing along the direction of travel.");
  const state = stateAt(replay, slowTime);
  const heading = state.q ? eulerOf(state.q).yaw : null;

  return (
    <div className="replay">
      {incident && (
        <IncidentPanel incident={incident} clock={clock} flightId={flightId} readOnly={readOnly}
          onExit={() => { clock.setLoop(null); exitIncident(); }} />
      )}
      <div className="replay-stage">
        <section className="card replay-scene" aria-label="3D view">
          <Scene3D replay={replay} clock={clock} findings={findings} camera={camera} notes={notes} />
          <div className="scene-bar">
            <div className="segmented" role="group" aria-label="Camera">
              {CAMERAS.map((c) => (
                <button key={c.mode} aria-pressed={camera === c.mode} onClick={() => setCamera(c.mode)} title={`${c.label} (${c.key})`}>
                  {c.label}
                </button>
              ))}
            </div>
            {camera === "orbit" && <span className="hint">drag to rotate · scroll to zoom</span>}
          </div>
          {sceneNotes.length > 0 && (
            <div className="scene-note" role="status">{sceneNotes.map((n) => <div key={n}>{n}</div>)}</div>
          )}
        </section>
        {(panels.video || panels.hud || panels.map) && (
          <aside className="replay-side">
            {panels.video && (
              <VideoPanel videos={videos} setVideos={setVideos} selected={selectedVideo} setSelected={setSelectedVideo}
                clock={clock} flightId={flightId} logStart={logStart} readOnly={readOnly} />
            )}
            {panels.hud && <section className="card card-body"><Hud replay={replay} clock={clock} /></section>}
            {panels.map && track && track.lat.length > 1 && (
              <section className="card card-body" aria-label="Map">
                <TrackView track={track} marker={slowTime} onPick={(t) => clock.seek(t)} compact
                  origin={meta.origin} heading={heading} />
              </section>
            )}
          </aside>
        )}
      </div>

      <section className="card card-body replay-controls" aria-label="Playback">
        <div className="controls-row">
          <button className="btn primary play" onClick={() => clock.toggle()} aria-label={playing ? "Pause" : "Play"} title="Space or K">
            {playing ? "❚❚" : "▶"}
          </button>
          <label className="rate">
            <span className="sr-only">Speed</span>
            <select value={rate} onChange={(e) => clock.setRate(Number(e.target.value))} aria-label="Playback speed" title="J slower · L faster">
              {RATES.map((r) => <option key={r} value={r}>{r}×</option>)}
            </select>
          </label>
          <TimeReadout clock={clock} />
          {loop && (
            <button className="btn ghost" onClick={() => clock.setLoop(null)} title="Stop looping">
              Loop {fmtTime(loop[0])}–{fmtTime(loop[1])} ✕
            </button>
          )}
          <span className="hint grow">shift-drag the timeline to loop a range</span>
          <div className="segmented" role="group" aria-label="Panels">
            {PANELS.map((p) => (
              <button key={p} aria-pressed={panels[p]} onClick={() => togglePanel(p)}>
                {p === "video" ? "Video" : p === "hud" ? "Instruments" : p === "map" ? "Map" : p === "charts" ? "Charts" : p === "list" ? "List" : "Notes"}
              </button>
            ))}
          </div>
        </div>
        <Timeline duration={meta.end} events={events} findings={findings} cursor={slowTime}
          onPick={(t) => clock.seek(t)} loop={loop} onLoop={(r) => clock.setLoop(r)} gaps={meta.gaps}
          coverage={covered ? [covered] : undefined} notes={notes} />
      </section>

      {panels.charts && (
        <ReplayCharts flightId={flightId} time={slowTime} follow={follow} setFollow={setFollow}
          clock={clock} duration={meta.end} motors={meta.motors} events={events} />
      )}
      {(panels.list || panels.notes) && (
        <div className="replay-bottom">
          {panels.notes && (
            <NotesPanel notes={notes} setNotes={setNotes} flightId={flightId} clock={clock} camera={camera}
              setCamera={(c) => CAMERAS.some((x) => x.mode === c) && setCamera(c as CameraMode)}
              readOnly={readOnly} composeAt={composeAt} setComposeAt={setComposeAt} />
          )}
          {panels.list && <MomentList findings={findings} events={events} time={slowTime} clock={clock} />}
        </div>
      )}
    </div>
  );
}

/** mm:ss.t, rounded down to the tenth: 40.79 s is 00:40.7 (fmtTime rounds whole seconds). */
export function fmtTenths(t: number): string {
  const tenths = Math.floor(Math.max(0, t) * 10 + 1e-6);
  return `${fmtTime(Math.floor(tenths / 10))}.${tenths % 10}`;
}

function TimeReadout({ clock }: { clock: Clock }) {
  const t = useClock(clock, (s) => s.time);
  const end = useClock(clock, (s) => s.end);
  return (
    <span className="num time-readout" aria-live="off">
      {fmtTenths(t)} <span className="faint">/ {fmtTime(end)}</span>
    </span>
  );
}

const CHARTS: { title: string; names: (motors: number) => string[] }[] = [
  { title: "Attitude", names: () => ["att.roll", "att.pitch"] },
  { title: "Height", names: () => ["pos.rel_alt"] },
  { title: "Battery", names: () => ["bat.volt"] },
  { title: "Motors", names: (n) => Array.from({ length: Math.min(n, 8) }, (_, i) => `mot.${i + 1}`) },
];
const COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

function ReplayCharts({ flightId, time, follow, setFollow, clock, duration, motors, events }: {
  flightId: string; time: number; follow: boolean; setFollow: (f: boolean) => void; clock: Clock;
  duration: number; motors: number; events: ReturnType<typeof useFlight>["events"];
}) {
  const [data, setData] = useState<Record<string, SeriesData>>({});
  const names = useMemo(() => CHARTS.flatMap((c) => c.names(motors)), [motors]);
  useEffect(() => {
    let alive = true;
    api.signals(flightId, names, undefined, 3000).then((r) => {
      if (alive) setData(Object.fromEntries(r.signals.map((s) => [s.name, s])));
    }, () => undefined);
    return () => {
      alive = false;
    };
  }, [flightId, names]);
  const modes = events.filter((e) => e.kind === "mode");
  const bands = modes.map((e, i) => ({ start: e.t, end: i + 1 < modes.length ? modes[i + 1].t : duration, label: e.text }));
  const range: [number, number] | null = follow ? [time - FOLLOW_WINDOW_S / 2, time + FOLLOW_WINDOW_S / 2] : null;
  return (
    <section className="card card-body replay-charts" aria-label="Charts">
      <div className="card-head" style={{ padding: 0, marginBottom: 8 }}>
        <h2>Charts</h2>
        <label className="hint">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow the replay ({FOLLOW_WINDOW_S} s window)
        </label>
      </div>
      <div className="chart-grid">
        {CHARTS.map((c) => {
          const series: Series[] = c.names(motors)
            .map((n, i) => ({ s: data[n], i }))
            .filter(({ s }) => s && !s.missing && s.t)
            .map(({ s, i }) => ({ name: s.name, unit: s.unit, t: s.t!, v: s.v!, color: COLORS[i % COLORS.length], dash: i >= 4 }));
          if (series.length === 0) return null;
          return (
            <div key={c.title}>
              <div className="eyebrow">{c.title}</div>
              <Chart series={series} height={140} marker={time} bands={bands} onPick={(t) => clock.seek(t)}
                xRange={range} domain={[0, duration]} syncKey={`replay-${flightId}`} />
            </div>
          );
        })}
      </div>
    </section>
  );
}

function MomentList({ findings, events, time, clock }: {
  findings: ReturnType<typeof useFlight>["findings"]; events: ReturnType<typeof useFlight>["events"]; time: number; clock: Clock;
}) {
  const items = useMemo(() => {
    const f = findings
      .filter((x) => x.start != null && (x.severity === "problem" || x.severity === "attention"))
      .map((x) => ({ t: x.start!, end: x.end, kind: "finding" as const, sev: x.severity, text: x.title, review: x.review?.status }));
    const e = events
      .filter((x) => x.kind !== "message" || x.level !== "info")
      .map((x) => ({ t: x.t, end: null, kind: "event" as const, sev: x.level === "critical" ? "problem" : x.level === "error" || x.level === "warning" ? "attention" : null, text: x.text, review: undefined }));
    return [...f, ...e].sort((a, b) => a.t - b.t);
  }, [findings, events]);
  const current = useRef<HTMLLIElement>(null);
  const active = items.reduce((best, it, i) => (it.t <= time ? i : best), -1);
  return (
    <section className="card replay-list" aria-label="Events and findings">
      <div className="card-head"><h2>Events and findings</h2><span className="hint">click to jump · findings start {LEAD_S} s early</span></div>
      <ul className="moments">
        {items.map((it, i) => (
          <li key={i} ref={i === active ? current : undefined} className={i === active ? "active" : undefined}>
            <button onClick={() => { clock.seek(it.kind === "finding" ? it.t - LEAD_S : it.t); }}>
              <span className="num faint">{it.kind === "finding" ? fmtWindow(it.t, it.end ?? null) : fmtTime(it.t)}</span>
              {it.sev ? (
                <span className={`sev ${it.sev === "problem" ? "sev-problem" : "sev-attention"}`}><SevIcon severity={it.sev as "problem" | "attention"} size={14} /></span>
              ) : <span className="dot" />}
              <span className={it.kind === "finding" ? "strong" : undefined}>{it.text}</span>
              {it.review && <span className="chip">{it.review.replace("_", " ")}</span>}
            </button>
          </li>
        ))}
        {items.length === 0 && <li className="empty">No events or findings in this flight.</li>}
      </ul>
    </section>
  );
}
