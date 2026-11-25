// The flight's debrief: a written record next to the automatic verdict.
// Status, summary, conclusion and follow-up actions; the notes written during
// replay and the engineer's finding reviews alongside; a printable report.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { REVIEW_LABEL } from "../api";
import { useReadOnly } from "../components/server";
import { SevIcon } from "../components/Severity";
import { fmtTime, fmtWindow } from "../format";
import { notesApi, rememberAuthor, rememberedAuthor, type Debrief, type Note } from "../replay/notes";
import { useFlight } from "./FlightPage";

export function DebriefView() {
  const { flight, findings } = useFlight();
  const readOnly = useReadOnly();
  const [debrief, setDebrief] = useState<Debrief | null | undefined>(undefined);
  const [notes, setNotes] = useState<Note[]>([]);
  const [form, setForm] = useState({ status: "open" as Debrief["status"], summary: "", conclusion: "", actions: [] as string[], author: "" });
  const [saved, setSaved] = useState<"saved" | "saving" | "dirty" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    notesApi.debrief(flight.id).then((d) => {
      setDebrief(d);
      setForm({
        status: d?.status ?? "open",
        summary: d?.summary ?? "",
        conclusion: d?.conclusion ?? "",
        actions: d?.actions ?? [],
        author: d?.author ?? rememberedAuthor(),
      });
    }, (e: Error) => setError(e.message));
    notesApi.list(flight.id).then(setNotes, () => undefined);
  }, [flight.id]);

  const change = (patch: Partial<typeof form>) => {
    setForm({ ...form, ...patch });
    setSaved("dirty");
  };
  const save = async () => {
    setSaved("saving");
    setError(null);
    rememberAuthor(form.author);
    try {
      const d = await notesApi.saveDebrief(flight.id, {
        ...form,
        actions: form.actions.filter((a) => a.trim()),
        author: form.author.trim() || null,
      });
      setDebrief(d);
      setSaved("saved");
    } catch (e) {
      setError((e as Error).message);
      setSaved("dirty");
    }
  };

  if (debrief === undefined && !error) return <div className="empty">Loading…</div>;
  const reviewed = findings.filter((f) => f.review);
  const issues = findings.filter((f) => f.severity === "problem" || f.severity === "attention");

  return (
    <div className="debrief">
      <section className="card card-body debrief-form" aria-label="Debrief">
        <div className="debrief-head">
          <h2>Debrief</h2>
          <div className="segmented" role="group" aria-label="Debrief status">
            {(["open", "closed"] as const).map((s) => (
              <button key={s} aria-pressed={form.status === s} disabled={readOnly} onClick={() => change({ status: s })}>
                {s === "open" ? "Open" : "Closed"}
              </button>
            ))}
          </div>
          <a className="btn ghost" href={`/api/flights/${flight.id}/debrief.html`} target="_blank" rel="noopener"
            title="Printable debrief report: flight facts, debrief, timeline with notes, track, provenance">
            Debrief report
          </a>
        </div>
        {!debrief && !readOnly && (
          <p className="hint">This flight has no debrief yet. Write what happened and what to do about it; the notes from the replay and the finding reviews are listed below for reference.</p>
        )}
        <label>
          <span className="eyebrow">Summary</span>
          <textarea rows={4} value={form.summary} readOnly={readOnly} maxLength={5000}
            placeholder="What happened on this flight" onChange={(e) => change({ summary: e.target.value })} />
        </label>
        <label>
          <span className="eyebrow">Conclusion</span>
          <textarea rows={3} value={form.conclusion} readOnly={readOnly} maxLength={5000}
            placeholder="Why it happened, as far as the evidence shows" onChange={(e) => change({ conclusion: e.target.value })} />
        </label>
        <div>
          <span className="eyebrow">Actions</span>
          <ul className="actions-list">
            {form.actions.map((a, i) => (
              <li key={i}>
                <input value={a} readOnly={readOnly} maxLength={300} aria-label={`Action ${i + 1}`}
                  onChange={(e) => change({ actions: form.actions.map((x, k) => (k === i ? e.target.value : x)) })} />
                {!readOnly && (
                  <button className="btn ghost" aria-label={`Remove action ${i + 1}`}
                    onClick={() => change({ actions: form.actions.filter((_, k) => k !== i) })}>✕</button>
                )}
              </li>
            ))}
          </ul>
          {!readOnly && form.actions.length < 30 && (
            <button className="btn ghost" onClick={() => change({ actions: [...form.actions, ""] })}>+ Add an action</button>
          )}
        </div>
        {!readOnly && (
          <div className="debrief-save">
            <input value={form.author} maxLength={80} placeholder="Your name" aria-label="Author"
              onChange={(e) => change({ author: e.target.value })} />
            <span className="grow" />
            {error && <span className="video-error" role="alert">{error}</span>}
            <span className="hint">{saved === "saving" ? "Saving…" : saved === "saved" ? "Saved" : saved === "dirty" ? "Not saved" : debrief ? `Last saved ${new Date(debrief.updated_at * 1000).toLocaleString()}` : ""}</span>
            <button className="btn primary" onClick={save} disabled={saved === "saving"}>Save debrief</button>
          </div>
        )}
        {readOnly && debrief?.author && <p className="hint">By {debrief.author}</p>}
      </section>

      <section className="card debrief-side" aria-label="Notes and findings">
        <div className="card-head"><h2>Notes from the replay <span className="count">{notes.length}</span></h2></div>
        <ul className="moments">
          {notes.map((n) => (
            <li key={n.id}>
              <Link to={`/flights/${flight.id}/replay?t=${n.t_start.toFixed(1)}`} className="moment-link">
                <span className="num faint">{fmtWindow(n.t_start, n.t_end)}</span>
                <span>{n.text}</span>
                {n.tags.map((t) => <span key={t} className="chip">{t}</span>)}
              </Link>
            </li>
          ))}
          {notes.length === 0 && (
            <li className="empty">No notes. In the Replay tab, press N to note what you see at that moment.</li>
          )}
        </ul>
        <div className="card-head"><h2>Findings</h2><span className="hint">{reviewed.length} of {issues.length} reviewed</span></div>
        <ul className="moments">
          {issues.map((f, i) => (
            <li key={i}>
              <Link to={f.start != null ? `/flights/${flight.id}/replay?t=${Math.max(0, f.start - 5).toFixed(1)}` : `/flights/${flight.id}`} className="moment-link">
                <span className="num faint">{f.start != null ? fmtTime(f.start) : "—"}</span>
                <span className={`sev ${f.severity === "problem" ? "sev-problem" : "sev-attention"}`}><SevIcon severity={f.severity} size={14} /></span>
                <span>{f.title}</span>
                {f.review && <span className="chip">{REVIEW_LABEL[f.review.status]}</span>}
              </Link>
            </li>
          ))}
          {issues.length === 0 && <li className="empty">No problems or attention findings.</li>}
        </ul>
      </section>
    </div>
  );
}
