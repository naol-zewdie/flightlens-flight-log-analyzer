// Time-stamped notes during a replay: add at the cursor (N) or for the looped
// range, tag, edit, delete; clicking a note jumps there and restores its view.

import { useEffect, useRef, useState } from "react";
import { fmtTime, fmtWindow } from "../format";
import type { Clock } from "./clock";
import { notesApi, rememberAuthor, rememberedAuthor, STARTER_TAGS, type Note } from "./notes";

interface Props {
  notes: Note[];
  setNotes: (n: Note[]) => void;
  flightId: string;
  clock: Clock;
  camera: string;
  setCamera: (c: string) => void;
  readOnly: boolean;
  /** Set by the N key: open the editor for a new note at this moment. */
  composeAt: { t: number; end: number | null } | null;
  setComposeAt: (c: { t: number; end: number | null } | null) => void;
}

export function NotesPanel({ notes, setNotes, flightId, clock, camera, setCamera, readOnly, composeAt, setComposeAt }: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loop = clock.get().loop;

  const sorted = [...notes].sort((a, b) => a.t_start - b.t_start);
  const open = (n: Note) => {
    clock.pause();
    clock.seek(n.t_start);
    if (n.view?.camera) setCamera(n.view.camera);
  };

  const save = async (draft: Draft, id?: string) => {
    setError(null);
    rememberAuthor(draft.author);
    const body = { text: draft.text, tags: draft.tags, author: draft.author || null };
    try {
      if (id) {
        const n = await notesApi.patch(id, body);
        setNotes(notes.map((x) => (x.id === id ? n : x)));
        setEditing(null);
      } else if (composeAt) {
        const n = await notesApi.add(flightId, { ...body, t_start: composeAt.t, t_end: composeAt.end, view: { camera } });
        setNotes([...notes, n]);
        setComposeAt(null);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (n: Note) => {
    if (!confirm("Delete this note?")) return;
    await notesApi.remove(n.id);
    setNotes(notes.filter((x) => x.id !== n.id));
  };

  return (
    <section className="card replay-notes" aria-label="Notes">
      <div className="card-head">
        <h2>Notes <span className="count">{notes.length}</span></h2>
        {!readOnly && !composeAt && (
          <div className="notes-add">
            <button className="btn" onClick={() => setComposeAt({ t: clock.get().time, end: null })} title="N">
              Note at {fmtTime(clock.get().time)}
            </button>
            {loop && (
              <button className="btn ghost" onClick={() => setComposeAt({ t: loop[0], end: loop[1] })}>
                Note for {fmtWindow(loop[0], loop[1])}
              </button>
            )}
          </div>
        )}
      </div>
      {error && <div className="video-error" role="alert" style={{ margin: "0 16px" }}>{error}</div>}
      {composeAt && !readOnly && (
        <NoteEditor
          heading={`New note at ${fmtWindow(composeAt.t, composeAt.end)}`}
          initial={{ text: "", tags: [], author: rememberedAuthor() }}
          onSave={(d) => save(d)}
          onCancel={() => setComposeAt(null)}
        />
      )}
      <ul className="notes">
        {sorted.map((n) =>
          editing === n.id ? (
            <li key={n.id}>
              <NoteEditor
                heading={`Note at ${fmtWindow(n.t_start, n.t_end)}`}
                initial={{ text: n.text, tags: n.tags, author: n.author ?? rememberedAuthor() }}
                onSave={(d) => save(d, n.id)}
                onCancel={() => setEditing(null)}
              />
            </li>
          ) : (
            <li key={n.id}>
              <button className="note-open" onClick={() => open(n)} title="Go to this moment (and the view it was written in)">
                <span className="num faint">{fmtWindow(n.t_start, n.t_end)}</span>
                <span className="note-text">{n.text}</span>
              </button>
              <div className="note-meta">
                {n.tags.map((t) => <span key={t} className="chip">{t}</span>)}
                {n.author && <span className="faint">{n.author}</span>}
                {!readOnly && (
                  <>
                    <button className="btn ghost" onClick={() => setEditing(n.id)}>Edit</button>
                    <button className="btn ghost danger" onClick={() => remove(n)}>Delete</button>
                  </>
                )}
              </div>
            </li>
          ),
        )}
        {notes.length === 0 && !composeAt && (
          <li className="empty">
            {readOnly ? "No notes on this flight." : "No notes yet. Press N during the replay to note what you see, at that moment."}
          </li>
        )}
      </ul>
    </section>
  );
}

interface Draft {
  text: string;
  tags: string[];
  author: string;
}

function NoteEditor({ heading, initial, onSave, onCancel }: {
  heading: string; initial: Draft; onSave: (d: Draft) => void; onCancel: () => void;
}) {
  const [d, setD] = useState<Draft>(initial);
  const [custom, setCustom] = useState("");
  const text = useRef<HTMLTextAreaElement>(null);
  useEffect(() => text.current?.focus(), []);
  const toggle = (t: string) => setD({ ...d, tags: d.tags.includes(t) ? d.tags.filter((x) => x !== t) : [...d.tags, t] });
  const tags = [...new Set([...STARTER_TAGS, ...d.tags])];
  return (
    <form className="note-editor" onSubmit={(e) => { e.preventDefault(); if (d.text.trim()) onSave(d); }}
      onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}>
      <div className="eyebrow">{heading}</div>
      <textarea ref={text} value={d.text} onChange={(e) => setD({ ...d, text: e.target.value })} rows={3} maxLength={2000}
        placeholder="What happened here, and why it matters" aria-label="Note"
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && d.text.trim()) onSave(d); }} />
      <div className="note-tags" role="group" aria-label="Tags">
        {tags.map((t) => (
          <button type="button" key={t} className="chip" aria-pressed={d.tags.includes(t)} onClick={() => toggle(t)}>{t}</button>
        ))}
        <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="+ tag" maxLength={32} aria-label="Add a tag"
          onKeyDown={(e) => {
            if (e.key === "Enter" && custom.trim()) {
              e.preventDefault();
              const t = custom.trim().toLowerCase();
              if (!d.tags.includes(t)) setD({ ...d, tags: [...d.tags, t] });
              setCustom("");
            }
          }} />
      </div>
      <div className="note-actions">
        <input value={d.author} onChange={(e) => setD({ ...d, author: e.target.value })} placeholder="Your name" maxLength={80} aria-label="Author" />
        <span className="grow" />
        <button type="button" className="btn ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn primary" disabled={!d.text.trim()}>Save note</button>
      </div>
    </form>
  );
}
