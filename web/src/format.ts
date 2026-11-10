// Number and time formatting shared by every screen.

export function fmtTime(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function fmtDuration(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  if (m < 60) return `${m} min ${sec} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

/** Engineering numbers: 15,869 · 43.4 · 3.52 · 0.84 */
export function fmtNum(x: number | null | undefined): string {
  if (x == null || !Number.isFinite(x)) return "—";
  const a = Math.abs(x);
  if (a >= 1000) return Math.round(x).toLocaleString("en-US");
  if (a >= 100) return x.toFixed(0);
  if (a >= 10 || Number.isInteger(x)) return String(Number(x.toFixed(1)));
  return x.toFixed(2);
}

export function fmtWindow(start: number | null, end: number | null): string {
  if (start == null) return "";
  if (end == null || end - start < 1) return fmtTime(start);
  return `${fmtTime(start)}–${fmtTime(end)}`;
}

export function fmtDate(iso: string | null, fallbackEpoch?: number): string {
  const d = iso ? new Date(iso) : fallbackEpoch ? new Date(fallbackEpoch * 1000) : null;
  if (!d || Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
