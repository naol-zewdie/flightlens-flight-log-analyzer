// The replay's playback clock: a tiny store outside React.
//
// Panels read it with useSyncExternalStore, so only what shows the time
// re-renders on each frame, not the whole flight page. One
// requestAnimationFrame loop advances the time while playing.

import { useSyncExternalStore } from "react";

export const RATES = [0.25, 0.5, 1, 2, 4, 8, 16] as const;

export interface ClockState {
  time: number;
  playing: boolean;
  rate: number;
  /** Looped range, if any */
  loop: [number, number] | null;
  start: number;
  end: number;
}

export interface Clock {
  get(): ClockState;
  subscribe(fn: () => void): () => void;
  seek(t: number): void;
  play(): void;
  pause(): void;
  toggle(): void;
  setRate(rate: number): void;
  faster(): void;
  slower(): void;
  setLoop(range: [number, number] | null): void;
  /** Advance by `dt` seconds of wall time (the animation loop calls this). */
  tick(dt: number): void;
  dispose(): void;
}

export function createClock(start: number, end: number, time = start): Clock {
  let s: ClockState = { time: clamp(time, start, end), playing: false, rate: 1, loop: null, start, end };
  const listeners = new Set<() => void>();
  let raf = 0;
  let last = 0;

  const set = (patch: Partial<ClockState>) => {
    s = { ...s, ...patch };
    listeners.forEach((fn) => fn());
  };

  const frame = (now: number) => {
    // Real time even at a low frame rate; but a gap over 1 s means the tab was in
    // the background, and the replay must not jump ahead when it comes back.
    let dt = last ? (now - last) / 1000 : 0;
    if (dt > 1) dt = 0;
    last = now;
    clock.tick(dt);
    raf = s.playing ? requestAnimationFrame(frame) : 0;
  };
  const startLoop = () => {
    if (!raf && typeof requestAnimationFrame !== "undefined") {
      last = 0;
      raf = requestAnimationFrame(frame);
    }
  };

  const clock: Clock = {
    get: () => s,
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    seek: (t) => set({ time: clamp(t, s.start, s.end) }),
    play() {
      if (s.playing) return;
      // at the end, playing starts over (from the loop start, if looping)
      const restart = s.time >= s.end ? (s.loop ? s.loop[0] : s.start) : s.time;
      set({ playing: true, time: restart });
      startLoop();
    },
    pause: () => set({ playing: false }),
    toggle: () => (s.playing ? clock.pause() : clock.play()),
    setRate: (rate) => set({ rate: clamp(rate, RATES[0], RATES[RATES.length - 1]) }),
    faster() {
      const next = RATES.find((r) => r > s.rate);
      if (next) set({ rate: next });
    },
    slower() {
      const prev = [...RATES].reverse().find((r) => r < s.rate);
      if (prev) set({ rate: prev });
    },
    setLoop(range) {
      if (range && range[1] - range[0] < 0.2) range = null;
      set({ loop: range ? [clamp(range[0], s.start, s.end), clamp(range[1], s.start, s.end)] : null });
    },
    tick(dt) {
      if (!s.playing || dt <= 0) return;
      let t = s.time + dt * s.rate;
      if (s.loop) {
        const [a, b] = s.loop;
        if (t > b || t < a) t = a + ((t - a) % (b - a) + (b - a)) % (b - a);
        set({ time: t });
      } else if (t >= s.end) {
        set({ time: s.end, playing: false });
      } else {
        set({ time: t });
      }
    },
    dispose() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      listeners.clear();
    },
  };
  return clock;
}

function clamp(x: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, x));
}

/** Re-render when the selected part of the clock changes. */
export function useClock<T>(clock: Clock, select: (s: ClockState) => T): T {
  return useSyncExternalStore(clock.subscribe, () => select(clock.get()));
}

/** Calls `fn(time)` at most every `ms` milliseconds while the clock changes (and once
 *  at the end of a burst), for things that need not move at 60 fps. */
export function throttleTime(clock: Clock, ms: number, fn: (t: number) => void): () => void {
  let lastRun = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastTime = NaN;
  const run = () => {
    timer = null;
    lastRun = performance.now();
    const t = clock.get().time;
    if (t !== lastTime) {
      lastTime = t;
      fn(t);
    }
  };
  const unsub = clock.subscribe(() => {
    if (timer) return;
    const wait = Math.max(0, ms - (performance.now() - lastRun));
    timer = setTimeout(run, wait);
  });
  return () => {
    unsub();
    if (timer) clearTimeout(timer);
  };
}
