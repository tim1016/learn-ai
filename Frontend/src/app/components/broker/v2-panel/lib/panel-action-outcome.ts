import { HttpErrorResponse } from '@angular/common/http';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';

export type PanelActionOutcome = 'success' | 'conflict' | 'failure' | 'unknown';

/**
 * Extracts the `detail` object from a `runBotAction` rejection body.
 *
 * The actions endpoint's error responses are untyped in the OpenAPI schema
 * (only 200/422 are generated — see `broker.types.ts`), so every caller must
 * narrow the body by hand. Centralized so panel shell and fleet list read the
 * same `{action_id, outcome, receipt_id, recorded_at_ms, message, why}` shape
 * instead of each re-deriving the type guard.
 */
export function extractActionErrorDetail(error: unknown): Record<string, unknown> | null {
  return error instanceof HttpErrorResponse &&
    typeof error.error === 'object' &&
    error.error !== null &&
    'detail' in error.error &&
    typeof error.error.detail === 'object' &&
    error.error.detail !== null
    ? (error.error.detail as Record<string, unknown>)
    : null;
}

export interface ActionRejection {
  readonly outcome: 'conflict' | 'failure' | 'unknown';
  readonly message: string;
  readonly why: string | null;
}

/** Parses a rejected `runBotAction` call's outcome, message, and remediation. */
export function deriveActionRejection(error: unknown, fallbackMessage: string): ActionRejection {
  const detail = extractActionErrorDetail(error);
  const outcome = detail?.['outcome'];
  return {
    outcome:
      outcome === 'conflict' || outcome === 'failure' || outcome === 'unknown' ? outcome : 'unknown',
    message:
      typeof detail?.['message'] === 'string'
        ? detail['message']
        : error instanceof Error
          ? error.message
          : fallbackMessage,
    why: typeof detail?.['why'] === 'string' ? detail['why'] : null,
  };
}

export interface ActionOutcomeToast {
  readonly severity: 'success' | 'warn' | 'error';
  readonly summary: string;
  readonly detail: string;
  readonly life: number;
}

/** The one toast shape for a completed `runBotAction` outcome, success or rejected. */
export function actionOutcomeToast(
  outcome: PanelActionOutcome,
  message: string,
  why: string | null = null,
): ActionOutcomeToast {
  return {
    severity: outcome === 'success' ? 'success' : outcome === 'conflict' ? 'warn' : 'error',
    summary: formatReceiptLabel(outcome),
    detail: why ? `${message} ${why}` : message,
    life: 6000,
  };
}
