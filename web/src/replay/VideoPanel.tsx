// A flight's video, following the replay clock once it is synced.
//
// Following: the video plays natively at the replay's speed; every 500 ms it is
// checked against the clock and corrected if it is more than 100 ms off (seeking
// on every frame would make it stutter). Paused, it seeks to the cursor.
// Sync mode detaches it: the video's own controls, offset nudges and sync points.

import { useEffect, useRef, useState } from "react";
import { fmtNum, fmtTime } from "../format";
import { useClock, type Clock } from "./clock";
import {
  coverage,
  creationTimeGuess,
  fitSync,
  isSynced,
  logToVideo,
  RATE_LIMITS,
  syncLabel,
  videoApi,
  type Video,
} from "./video";

const DRIFT_S = 0.1;
const CHECK_MS = 500;

interface Props {
  videos: Video[];
  setVideos: (v: Video[]) => void;
  selected: string | null;
  setSelected: (id: string | null) => void;
  clock: Clock;
  flightId: string;
  logStart: string | null;
  readOnly: boolean;
}

export function VideoPanel({ videos, setVideos, selected, setSelected, clock, flightId, logStart, readOnly }: Props) {
  const video = videos.find((v) => v.id === selected) ?? null;
  const el = useRef<HTMLVideoElement>(null);
  const [syncing, setSyncing] = useState(false);
  const [upload, setUpload] = useState<{ name: string; progress: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outside, setOutside] = useState(false);
  const [muted, setMuted] = useState(true);
  const time = useClock(clock, (s) => s.time);

  const replace = (v: Video) => setVideos(videos.map((x) => (x.id === v.id ? v : x)));
  const save = async (changes: Parameters<typeof videoApi.patch>[1]) => {
    if (!video || readOnly) return;
    try {
      replace(await videoApi.patch(video.id, changes));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // follow the clock
  useEffect(() => {
    const v = el.current;
    if (!v || !video || !isSynced(video) || syncing) return;
    let lastCheck = 0;
    const follow = () => {
      const s = clock.get();
      const want = logToVideo(video, s.time);
      const length = video.duration_s ?? v.duration ?? Infinity;
      const off = want < 0 || want > length;
      setOutside(off);
      if (off) {
        if (!v.paused) v.pause();
        return;
      }
      const now = performance.now();
      if (s.playing) {
        v.playbackRate = Math.min(16, Math.max(0.0625, s.rate / video.rate));
        if (v.paused) {
          v.currentTime = want;
          v.play().catch(() => undefined); // autoplay rules: muted video may play
        } else if (now - lastCheck > CHECK_MS) {
          lastCheck = now;
          if (Math.abs(v.currentTime - want) > DRIFT_S) v.currentTime = want;
        }
      } else {
        if (!v.paused) v.pause();
        const frame = 1 / (video.fps || 30);
        if (Math.abs(v.currentTime - want) > frame / 2) v.currentTime = want;
      }
    };
    follow();
    return clock.subscribe(follow);
  }, [clock, video, syncing]);

  useEffect(() => {
    if (el.current) el.current.muted = muted;
  }, [muted, video?.id]);

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    setUpload({ name: file.name, progress: 0 });
    try {
      const { video: v } = await videoApi.upload(flightId, file, (progress) => setUpload({ name: file.name, progress }));
      setVideos([...videos.filter((x) => x.id !== v.id), v]);
      setSelected(v.id);
      if (!isSynced(v)) setSyncing(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUpload(null);
    }
  };

  const remove = async () => {
    if (!video || !confirm(`Remove the video “${video.filename}” from this flight? The file is deleted.`)) return;
    await videoApi.remove(video.id);
    const rest = videos.filter((v) => v.id !== video.id);
    setVideos(rest);
    setSelected(rest[0]?.id ?? null);
    setSyncing(false);
  };

  return (
    <section className="card card-body video-panel" aria-label="Video">
      <div className="video-head">
        <h2>Video</h2>
        {videos.length > 1 && (
          <select value={selected ?? ""} onChange={(e) => setSelected(e.target.value)} aria-label="Shown video">
            {videos.map((v) => <option key={v.id} value={v.id}>{v.label || v.filename}</option>)}
          </select>
        )}
        {!readOnly && (
          <label className="btn ghost" title="Attach an MP4 or WebM video of this flight">
            Add video
            <input type="file" accept="video/mp4,video/webm,.mp4,.webm" className="sr-only"
              onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = ""; }} />
          </label>
        )}
      </div>

      {upload && (
        <div className="hint">Uploading {upload.name}… {Math.round(upload.progress * 100)}%
          <div className="progress" style={{ marginTop: 4 }}><div style={{ width: `${upload.progress * 100}%` }} /></div>
        </div>
      )}
      {error && <div className="video-error" role="alert">{error}</div>}

      {!video ? (
        !upload && <p className="muted">No video for this flight. Add the onboard or ground recording; once synced, it plays in step with the replay.</p>
      ) : (
        <>
          <div className="video-frame">
            <video
              key={video.id}
              ref={el}
              src={videoApi.fileUrl(video.id)}
              controls={syncing || !isSynced(video)}
              playsInline
              preload="metadata"
              onLoadedMetadata={(e) => {
                const d = e.currentTarget.duration;
                if (!video.duration_s && Number.isFinite(d) && !readOnly) save({ duration_s: d });
              }}
            />
            {outside && !syncing && isSynced(video) && <div className="video-outside">No video at this time</div>}
          </div>
          <div className="video-status">
            <span className={isSynced(video) ? "muted" : "attention-ink"}>{syncLabel(video)}</span>
            {isSynced(video) && !syncing && (
              <span className="num faint">video {fmtTime(Math.max(0, logToVideo(video, time)))}</span>
            )}
          </div>
          <div className="video-actions">
            <button className="btn ghost" onClick={() => setMuted(!muted)} aria-pressed={!muted}>{muted ? "Sound off" : "Sound on"}</button>
            {!readOnly && (
              <button className="btn" onClick={() => setSyncing(!syncing)} aria-pressed={syncing}>
                {syncing ? "Done syncing" : "Sync…"}
              </button>
            )}
            {!readOnly && <button className="btn ghost danger" onClick={remove}>Remove</button>}
          </div>
          {syncing && !readOnly && (
            <SyncTools video={video} videoEl={el} clock={clock} logStart={logStart} save={save} />
          )}
        </>
      )}
    </section>
  );
}

function SyncTools({ video, videoEl, clock, logStart, save }: {
  video: Video;
  videoEl: React.RefObject<HTMLVideoElement>;
  clock: Clock;
  logStart: string | null;
  save: (c: Parameters<typeof videoApi.patch>[1]) => Promise<void>;
}) {
  const frame = 1 / (video.fps || 30);
  const offset = video.offset_s ?? 0;
  const guess = creationTimeGuess(video.created_at_media, logStart);
  const fit = fitSync(video.sync_points);
  const fitBad = fit && (fit.rate < RATE_LIMITS[0] || fit.rate > RATE_LIMITS[1]);

  const nudge = (d: number) => save({ offset_s: offset + d, sync_method: "manual" });
  const addPoint = () => {
    const v = videoEl.current;
    if (!v) return;
    const points = [...video.sync_points, [v.currentTime, clock.get().time] as [number, number]];
    const f = fitSync(points)!;
    const ok = f.rate >= RATE_LIMITS[0] && f.rate <= RATE_LIMITS[1];
    save(ok ? { sync_points: points, offset_s: f.offset, rate: f.rate, sync_method: "points" } : { sync_points: points });
  };
  const removePoint = (i: number) => {
    const points = video.sync_points.filter((_, k) => k !== i);
    const f = fitSync(points);
    save(f ? { sync_points: points, offset_s: f.offset, rate: f.rate, sync_method: "points" }
           : { sync_points: points, sync_method: video.offset_s == null ? "none" : "manual" });
  };

  return (
    <div className="sync-tools">
      <p className="hint">
        Find a sharp moment in both: take-off, a quick turn, landing. Put the video on it (its own
        controls, or the frame buttons) and the replay on it (timeline, arrow keys), then add a sync
        point. One point sets the offset; two or more, far apart, also correct a camera clock that runs fast or slow.
      </p>
      <div className="sync-row">
        <span className="eyebrow">Offset</span>
        <span className="num">{video.offset_s == null ? "—" : `${fmtNum(offset)} s`}</span>
        {[-1, -0.1, -frame, frame, 0.1, 1].map((d) => (
          <button key={d} className="btn ghost" onClick={() => nudge(d)}
            title={`Video ${d > 0 ? "later" : "earlier"} relative to the log`}>
            {d > 0 ? "+" : "−"}{Math.abs(d) === frame ? "1 frame" : `${Math.abs(d)} s`}
          </button>
        ))}
      </div>
      <div className="sync-row">
        <span className="eyebrow">Video frame</span>
        <button className="btn ghost" onClick={() => { const v = videoEl.current; if (v) v.currentTime = Math.max(0, v.currentTime - frame); }}>◀ frame</button>
        <button className="btn ghost" onClick={() => { const v = videoEl.current; if (v) v.currentTime += frame; }}>frame ▶</button>
        <button className="btn primary" onClick={addPoint}>Add sync point here</button>
      </div>
      {video.sync_points.length > 0 && (
        <ul className="sync-points">
          {video.sync_points.map(([v, l], i) => (
            <li key={i}>
              <span className="num">video {fmtTime(v)}.{String(Math.round((v % 1) * 100)).padStart(2, "0")} = log {fmtTime(l)}.{String(Math.round((l % 1) * 100)).padStart(2, "0")}</span>
              {fit && video.sync_points.length > 1 && (
                <span className="faint num"> · {Math.round((l - fit.offset - fit.rate * v) * 1000)} ms off the fit</span>
              )}
              <button className="btn ghost" onClick={() => removePoint(i)} aria-label={`Remove sync point ${i + 1}`}>✕</button>
            </li>
          ))}
        </ul>
      )}
      {fitBad && (
        <div className="video-error" role="alert">
          These points do not agree (the clocks would differ by {fmtNum(Math.abs(fit!.rate - 1) * 100)}%): one is probably
          on a different moment. Remove it.
        </div>
      )}
      {guess != null && (
        <button className="btn ghost" onClick={() => save({ offset_s: guess, rate: 1, sync_method: "metadata" })}
          title="From the time the camera wrote into the file; often wrong by seconds">
          Use the camera's clock as a first guess ({fmtNum(guess)} s)
        </button>
      )}
      {coverage(video) && (
        <p className="hint">This video covers {fmtTime(coverage(video)![0])}–{fmtTime(coverage(video)![1])} of the log.</p>
      )}
    </div>
  );
}
