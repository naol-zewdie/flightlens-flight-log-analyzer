import { useEffect, useState } from "react";
import { api, REVIEW_LABEL, type Finding, type ReviewStatus } from "../api";
import { fmtDate } from "../format";

const ORDER: ReviewStatus[] = ["confirmed", "false_alarm", "accepted"];
const HELP: Record<ReviewStatus, string> = {
  confirmed: "The problem is real",
  false_alarm: "FlightLens was wrong here",
  accepted: "Real, but acceptable for this aircraft or mission",
};

/** An engineer's verdict on one finding. Never changes the automatic verdict. */
export function ReviewBox({
  flightId,
  finding,
  readOnly,
  onSaved,
}: {
  flightId: string;
  finding: Finding;
  readOnly: boolean;
  onSaved: () => void;
}) {
  const saved = finding.review ?? null;
  const [status, setStatus] = useState<ReviewStatus | null>(saved?.status ?? null);
  const [note, setNote] = useState(saved?.note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Another finding selected, or the review reloaded: show what is stored.
  useEffect(() => {
    setStatus(saved?.status ?? null);
    setNote(saved?.note ?? "");
    setError(null);
  }, [finding.key, saved?.status, saved?.note]);

  if (!finding.key || !["problem", "attention"].includes(finding.severity)) return null;
  const changed = status !== (saved?.status ?? null) || note.trim() !== (saved?.note ?? "");

  const run = (action: Promise<unknown>) => {
    setBusy(true);
    setError(null);
    action
      .then(onSaved)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  if (readOnly) {
    return saved ? (
      <div className="review-box">
        <div className="eyebrow">Engineer review</div>
        <div>
          <b>{REVIEW_LABEL[saved.status]}</b>
          {saved.note && <span className="muted"> — {saved.note}</span>}
        </div>
      </div>
    ) : null;
  }

  return (
    <div className="review-box" aria-label="Engineer review">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
        <div className="eyebrow">Engineer review</div>
        {saved && <span className="hint">saved {fmtDate(null, saved.reviewed_at)}</span>}
      </div>
      <div className="segmented" role="group" aria-label="Review">
        {ORDER.map((s) => (
          <button key={s} type="button" aria-pressed={status === s} title={HELP[s]} disabled={busy} onClick={() => setStatus(s)}>
            {REVIEW_LABEL[s]}
          </button>
        ))}
      </div>
      <textarea
        className="input"
        rows={2}
        placeholder="Note (optional): what you found, what was done"
        value={note}
        maxLength={2000}
        onChange={(e) => setNote(e.target.value)}
        aria-label="Review note"
      />
      {error && <div className="sev-problem" style={{ fontSize: 13 }}>{error}</div>}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button
          className="btn"
          type="button"
          disabled={busy || !status || !changed}
          onClick={() => status && run(api.setReview(flightId, finding.key!, status, note))}
        >
          {saved ? "Update review" : "Save review"}
        </button>
        {saved && (
          <button className="btn ghost" type="button" disabled={busy} onClick={() => run(api.clearReview(flightId, finding.key!))}>
            Clear
          </button>
        )}
        <span className="hint">Reviews never change the automatic verdict; they can be exported as validation labels.</span>
      </div>
    </div>
  );
}
