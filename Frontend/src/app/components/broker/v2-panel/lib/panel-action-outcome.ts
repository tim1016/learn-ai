import { HttpErrorResponse } from '@angular/common/http';
import type { PanelActionErrorResponse } from '../../../../api/broker-models';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';

export type PanelActionOutcome = 'success' | 'conflict' | 'failure' | 'unknown';

/**
 * Extracts the `detail` object from a rejected panel-action call.
 *
 * This one parser is shared across several distinct backend error bodies —
 * the `/actions` route's typed `PanelActionErrorResponse` (see
 * `broker.types.ts`) plus older untyped shapes (e.g. historical-recovery's
 * `{reason, message}`) — so it stays a permissive record rather than one
 * generated type; `deriveActionRejection` narrows the fields it needs.
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
  const reason = detail?.['reason'];
  const reasonCode = (detail as PanelActionErrorResponse | null)?.reason_code;
  return {
    outcome:
      outcome === 'conflict' || outcome === 'failure' || outcome === 'unknown' ? outcome : 'unknown',
    message:
      typeof detail?.['message'] === 'string'
        ? detail['message']
        : error instanceof Error
          ? error.message
          : fallbackMessage,
    // `why` is backend-authored prose, rendered as-is. Only when the backend
    // sent no prose does a raw code (`reason`, then the newer `reason_code`)
    // become the remediation text, and only then does it need receiptLabel.
    why:
      typeof detail?.['why'] === 'string'
        ? detail['why']
        : typeof reason === 'string'
          ? formatReceiptLabel(reason)
          : typeof reasonCode === 'string'
            ? formatReceiptLabel(reasonCode)
            : null,
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
