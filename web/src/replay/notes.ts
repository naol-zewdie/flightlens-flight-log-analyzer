// Notes about a flight (a moment or a range) and its debrief, and their API calls.

export interface Note {
  id: string;
  flight_id: string;
  t_start: number;
  t_end: number | null;
  text: string;
  tags: string[];
  /** What the author was looking at: restored when the note is opened. */
  view: { camera?: string } | null;
  author: string | null;
  created_at: number;
  updated_at: number;
}

export interface Debrief {
  flight_id: string;
  status: "open" | "closed";
  summary: string | null;
  conclusion: string | null;
  actions: string[];
  author: string | null;
  updated_at: number;
}

export const STARTER_TAGS = ["pilot", "mechanical", "software", "weather", "sensor", "procedure", "good practice"];

/** The author's name, remembered in this browser (FlightLens has no accounts). */
export function rememberedAuthor(): string {
  try {
    return localStorage.getItem("fl-author") ?? "";
  } catch {
    return "";
  }
}

export function rememberAuthor(name: string) {
  try {
    if (name.trim()) localStorage.setItem("fl-author", name.trim());
  } catch {
    /* storage unavailable */
  }
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map((d: { msg: string }) => d.msg).join("; ") : msg;
    } catch {
      /* not JSON */
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

const send = (method: string, body: unknown) => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

type NoteInput = Pick<Note, "t_start" | "t_end" | "text" | "tags" | "view" | "author">;

export const notesApi = {
  list: (flightId: string) =>
    fetch(`/api/flights/${flightId}/notes`).then((r) => json<{ notes: Note[] }>(r)).then((d) => d.notes),
  add: (flightId: string, note: NoteInput) =>
    fetch(`/api/flights/${flightId}/notes`, send("POST", note)).then((r) => json<{ note: Note }>(r)).then((d) => d.note),
  patch: (id: string, changes: Partial<NoteInput>) =>
    fetch(`/api/notes/${id}`, send("PATCH", changes)).then((r) => json<{ note: Note }>(r)).then((d) => d.note),
  remove: (id: string) => fetch(`/api/notes/${id}`, { method: "DELETE" }).then((r) => json<{ deleted: string }>(r)),
  debrief: (flightId: string) =>
    fetch(`/api/flights/${flightId}/debrief`).then((r) => json<{ debrief: Debrief | null }>(r)).then((d) => d.debrief),
  saveDebrief: (flightId: string, d: Pick<Debrief, "status" | "actions"> & { summary: string; conclusion: string; author: string | null }) =>
    fetch(`/api/flights/${flightId}/debrief`, send("PUT", d)).then((r) => json<{ debrief: Debrief }>(r)).then((x) => x.debrief),
};
