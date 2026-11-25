import { describe, expect, it } from "vitest";
import { coverage, creationTimeGuess, fitSync, logToVideo, syncLabel, videoToLog, type Video } from "./video";

const base: Video = {
  id: "v", flight_id: "f", filename: "a.mp4", file: "v.mp4", sha256: "x", size_bytes: 1, duration_s: 60,
  width: null, height: null, codec: null, fps: 30, created_at_media: null, offset_s: null, rate: 1,
  sync_method: "none", sync_points: [], label: null, uploaded_at: 0,
};

describe("video clock", () => {
  it("maps between video and log time both ways", () => {
    const v = { offset_s: 7.3, rate: 1.001 };
    expect(videoToLog(v, 10)).toBeCloseTo(17.31, 9);
    expect(logToVideo(v, videoToLog(v, 42.5))).toBeCloseTo(42.5, 9);
  });

  it("finds the offset from one point, and offset and drift from several", () => {
    expect(fitSync([])).toBeNull();
    expect(fitSync([[2, 9.3]])).toEqual({ offset: expect.closeTo(7.3, 9), rate: 1, rms: 0 });
    // a camera clock running 0.1% slow, marked at three flashes
    const truth = (v: number) => 7.3 + 1.001 * v;
    const fit = fitSync([[5, truth(5)], [30, truth(30)], [55, truth(55)]])!;
    expect(fit.offset).toBeCloseTo(7.3, 9);
    expect(fit.rate).toBeCloseTo(1.001, 9);
    expect(fit.rms).toBeLessThan(1e-9);
    // marking errors show up as the residual
    const noisy = fitSync([[5, truth(5) + 0.04], [30, truth(30) - 0.04], [55, truth(55) + 0.04]])!;
    expect(noisy.rms).toBeGreaterThan(0.03);
    // two points at the same video time: only the offset
    expect(fitSync([[5, 12], [5, 12.2]])!.rate).toBe(1);
  });

  it("covers the log range of a synced video", () => {
    expect(coverage(base)).toBeNull();
    expect(coverage({ ...base, offset_s: 10 })).toEqual([10, 70]);
    expect(coverage({ ...base, offset_s: 10, duration_s: null })).toBeNull();
  });

  it("guesses from the camera clock, and refuses absurd guesses", () => {
    expect(creationTimeGuess("2026-05-04T09:35:12Z", "2026-05-04T09:35:00Z")).toBe(12);
    expect(creationTimeGuess("2026-05-06T09:35:00Z", "2026-05-04T09:35:00Z")).toBeNull();
    expect(creationTimeGuess(null, "2026-05-04T09:35:00Z")).toBeNull();
  });

  it("says how the sync was found", () => {
    expect(syncLabel(base)).toBe("Not synced yet");
    expect(syncLabel({ ...base, sync_method: "metadata", offset_s: 3 })).toMatch(/Guessed/);
    const v = { ...base, sync_method: "points" as const, offset_s: 7.3, rate: 1.001, sync_points: [[5, 12.305], [55, 62.355]] as [number, number][] };
    expect(syncLabel(v)).toMatch(/^Synced from 2 points \(add a third to check them\), clock 0.10%/);
    const three = { ...v, sync_points: [...v.sync_points, [30, 37.33]] as [number, number][] };
    expect(syncLabel(three)).toMatch(/^Synced from 3 points, ±\d+ ms between them/);
  });
});
