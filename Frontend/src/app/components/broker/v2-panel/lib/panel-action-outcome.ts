import type { PanelActionErrorResponse } from '../../../../api/broker-models';
import { fleetRefusalCopyFor } from '../../../../fleet/fleet-refusal-copy';
import { refusalBody } from '../../../../shared/errors/refusal-body';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';

export type PanelActionOutcome = 'success' | 'conflict' | 'failure' | 'unknown';

/**
 * Extracts the refusal body from a rejected panel-action call.
 *
 * This one parser is shared across several distinct backend error bodies —
 * the `/actions` route's typed `PanelActionErrorResponse` (see
 * `broker.types.ts`), older untyped shapes (e.g. historical-recovery's
 * `{reason, message}`), and the broker clerk fleet's flat
 * `{reason, message, next_step}` — so it stays a permissive record rather than
 * one generated type; `deriveActionRejection` narrows the fields it needs.
 *
 * Shape handling lives in `refusalBody`, which knows that a fleet refusal is
 * flat while a FastAPI `HTTPException` nests under `detail`. This used to
 * require the nested shape, so every fleet refusal parsed as `null` and fell
 * back to generic prose.
 */
export function extractActionErrorDetail(error: unknown): Record<string, unknown> | null {
  return refusalBody(error);
}

export interface ActionRejection {
  readonly outcome: 'conflict' | 'failure' | 'unknown';
  readonly message: string;
  readonly why: string | null;
  /** The raw refusal code (`reason`, then `reason_code`), never passed through
   * `formatReceiptLabel`, for a caller that needs to branch on the refusal
   * kind rather than render it (e.g. a stale-generation refusal triggering a
   * directory refresh, #2068). */
  readonly reasonCode: string | null;
}

/** Parses a rejected `runBotAction` call's outcome, message, and remediation. */
export function deriveActionRejection(error: unknown, fallbackMessage: string): ActionRejection {
  const detail = extractActionErrorDetail(error);
  const outcome = detail?.['outcome'];
  const reason = detail?.['reason'];
  const typedReasonCode = (detail as PanelActionErrorResponse | null)?.reason_code;
  const reasonCode =
    typeof reason === 'string' ? reason : typeof typedReasonCode === 'string' ? typedReasonCode : null;
  // The fleet's flat `{reason, message, next_step}` body carries no `outcome`
  // field at all — only the `/actions` route's typed contract does. Without
  // this branch every fleet refusal (including the #2068 fence's own) fell
  // through to 'unknown' and rendered as the literal word "Unknown" (#2067,
  // #2102). `fleetRefusalCopyFor` knows each closed-vocabulary code's pinned
  // wire status and derives 'conflict' for a 409 (a state conflict the
  // caller must re-read and re-prepare) or 'failure' for anything else.
  const knownRefusal = fleetRefusalCopyFor(reasonCode);
  return {
    outcome:
      outcome === 'conflict' || outcome === 'failure' || outcome === 'unknown'
        ? outcome
        : (knownRefusal?.outcome ?? 'unknown'),
    message:
      typeof detail?.['message'] === 'string'
        ? detail['message']
        : knownRefusal !== null
          ? knownRefusal.message
          : error instanceof Error
            ? error.message
            : fallbackMessage,
    // `why` and the fleet's `next_step` are both backend-authored prose,
    // rendered as-is. Only when the backend sent no prose at all does the
    // client-authored fallback copy apply, and only when even that is
    // unavailable does a raw code become the remediation text via
    // receiptLabel, so nothing ever renders blank.
    why:
      typeof detail?.['why'] === 'string'
        ? detail['why']
        : typeof detail?.['next_step'] === 'string'
          ? detail['next_step']
          : knownRefusal !== null
            ? knownRefusal.nextStep
            : reasonCode !== null
              ? formatReceiptLabel(reasonCode)
              : null,
    reasonCode,
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
