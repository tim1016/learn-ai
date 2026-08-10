// TS mirror of PythonDataService/app/schemas/run_verdict.py.
// Python authors the frozen verdict; the frontend renders it verbatim.
export type RunVerdictGrade = "A+" | "A" | "B" | "C" | "D" | "F";
export type RunVerdictSignal = "Deploy" | "Paper-trade" | "Iterate" | "Rework" | "Reject";
export type RunVerdictEvidenceAction =
  | "Advance to independent validation"
  | "Continue forward and out-of-sample validation"
  | "Investigate identified weaknesses"
  | "Revise the hypothesis or validation design"
  | "Substantial rework is required"
  | "Rework the tested strategy hypothesis and validate independently"
  | "Inspect reconciliation discrepancies before relying on this evidence";
export type RunVerdictEngine = "python" | "lean";
export type RunVerdictStatus = "complete" | "incomplete" | "unavailable" | "failed";

export interface RunVerdictSubScore {
  key: string;
  label: string;
  score: number | null;
  raw_value: number | null;
  display: string;
  note: string;
}

export interface RunVerdictDimension {
  key: string;
  label: string;
  weight: number;
  score: number | null;
  summary: string;
  sub_scores: RunVerdictSubScore[];
}

export interface RunVerdictCleanliness {
  is_clean: boolean;
  is_reconciliation_grade: boolean;
  error_counts: Record<string, number>;
}

export interface RunVerdictMissingEvidence {
  key: string;
  label: string;
  producer: "platform";
  reason: string;
}

export interface RunVerdict {
  verdict_version: number;
  /** Optional only so historical persisted v1 verdicts continue to render. */
  status?: RunVerdictStatus;
  engine: RunVerdictEngine;
  generated_at_ms: number;
  composite: number | null;
  grade: RunVerdictGrade | null;
  evidence_action?: RunVerdictEvidenceAction | null;
  signal: RunVerdictSignal | null;
  headline: string;
  red_flags: string[];
  dimensions: RunVerdictDimension[];
  missing_metrics: string[];
  missing_required_metrics?: string[];
  missing_required_evidence?: RunVerdictMissingEvidence[];
  available_required_metrics?: number;
  required_metrics?: number;
  normalized_weights: boolean;
  cleanliness: RunVerdictCleanliness | null;
  /** Python-authored, tolerance-pinned receipt used for paired-run comparison. */
  parity_signature?: Record<string, unknown>;
}
