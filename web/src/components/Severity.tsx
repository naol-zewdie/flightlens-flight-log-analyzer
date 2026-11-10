import type { Severity, Verdict } from "../api";

// Severity is never colour alone: every state has its own shape and a word.

export function SevIcon({ severity, size = 16 }: { severity: Severity | Verdict; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  switch (severity) {
    case "problem":
      return (
        <svg {...common}>
          <path d="M5.2 1.5h5.6l3.7 3.7v5.6l-3.7 3.7H5.2l-3.7-3.7V5.2z" fill="currentColor" stroke="none" />
          <path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" stroke="#fff" />
        </svg>
      );
    case "attention":
      return (
        <svg {...common}>
          <path d="M8 1.6l6.6 12.1H1.4z" fill="currentColor" stroke="none" />
          <path d="M8 6v3.6" stroke="#1a1a19" />
          <circle cx="8" cy="11.6" r="0.4" stroke="#1a1a19" />
        </svg>
      );
    case "ok":
    case "healthy":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6.6" fill="currentColor" stroke="none" />
          <path d="M5.2 8.2l1.9 1.9 3.7-4" stroke="#fff" />
        </svg>
      );
    case "error":
      return (
        <svg {...common}>
          <rect x="1.8" y="1.8" width="12.4" height="12.4" rx="2" />
          <path d="M8 4.8v3.8M8 11.2v.01" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" strokeDasharray="2.2 2.2" />
        </svg>
      );
  }
}

const SEV_LABEL: Record<Severity, string> = {
  problem: "Problem",
  attention: "Attention",
  ok: "OK",
  not_evaluated: "Not evaluated",
  error: "Error",
};

export function SevLabel({ severity }: { severity: Severity }) {
  const cls =
    severity === "problem"
      ? "sev-problem"
      : severity === "attention"
        ? "sev-attention"
        : severity === "ok"
          ? "sev-ok"
          : "sev-neutral";
  return (
    <span className={`sev ${cls}`}>
      <SevIcon severity={severity} />
      {SEV_LABEL[severity]}
    </span>
  );
}

const VERDICT_LABEL: Record<Verdict, string> = {
  healthy: "Healthy",
  attention: "Attention",
  problem: "Problem",
  "insufficient data": "Insufficient data",
};

export function verdictClass(v: Verdict | null): string {
  if (v === "problem") return "problem";
  if (v === "attention") return "attention";
  if (v === "healthy") return "healthy";
  return "neutral";
}

export function VerdictPill({ verdict }: { verdict: Verdict | null }) {
  if (!verdict) return <span className="pill neutral">—</span>;
  return (
    <span className={`pill ${verdictClass(verdict)}`}>
      <SevIcon severity={verdict === "insufficient data" ? "not_evaluated" : verdict} size={14} />
      {VERDICT_LABEL[verdict]}
    </span>
  );
}

export { VERDICT_LABEL };
