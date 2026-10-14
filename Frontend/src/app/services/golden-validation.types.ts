/**
 * Wire contract for the immutable Validation Golden Run workflow.
 *
 * Python owns the scientific case and the computed parity evidence.  The UI
 * only transports and presents that evidence alongside the human judgment.
 *
 * Every shape derives from the generated contract (`api/broker.types.ts`), so
 * a field the server adds, drops or retypes is a compile error here. The
 * contract types `validation_case` and `parity_evidence` as empty objects, so
 * those two are the only hand-typed members.
 */

import type { components } from "../api/broker.types";

type Schemas = components["schemas"];

export type GoldenReview = Schemas["GoldenReviewResponse"];
export type GoldenReviewDecision = GoldenReview["decision"];
export type DesignateGoldenValidationRequest = Schemas["DesignateGoldenRunRequest"];
export type ReviewGoldenValidationRequest = Schemas["ReviewGoldenRunRequest"];

export interface GoldenValidationCase {
  schema_version: number;
  source_run_id: number;
  strategy: { name: string; program_version: string | null };
  symbol: string;
  parameters: Record<string, unknown>;
  window: { start_ms: number; end_ms: number; timespan: string };
  data_policy: Record<string, unknown> | null;
  execution: {
    fill_mode: string;
    initial_cash: number;
    commission_per_order: number | null;
    brokerage_policy: string | null;
    configuration: Record<string, unknown> | null;
  };
  requested_engine: string | null;
  parity_group_id: string | null;
}

export type GoldenValidation = Omit<Schemas["GoldenValidationResponse"], "validation_case" | "parity_evidence"> & {
  validation_case: GoldenValidationCase;
  parity_evidence: Record<string, unknown>;
};
