// TS mirror of PythonDataService/app/schemas/run_verdict.py.
// Python authors the frozen verdict; the frontend renders it verbatim.
export type RunVerdictGrade = "A+" | "A" | "B" | "C" | "D" | "F";
export type RunVerdictSignal = "Deploy" | "Paper-trade" | "Iterate" | "Rework" | "Reject";
export type RunVerdictEngine = "python" | "lean";

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

export interface RunVerdict {
  verdict_version: number;
  engine: RunVerdictEngine;
  generated_at_ms: number;
  composite: number | null;
  grade: RunVerdictGrade | null;
  signal: RunVerdictSignal | null;
  headline: string;
  red_flags: string[];
  dimensions: RunVerdictDimension[];
  missing_metrics: string[];
  normalized_weights: boolean;
  cleanliness: RunVerdictCleanliness | null;
}
