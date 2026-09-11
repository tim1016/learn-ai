// Reads the broker-configuration surface's refusal contract
// (`docs/architecture/broker-configuration-profile-contract.md` §6): a `detail`
// object carrying a code-like `reason` plus backend-authored `message` and an
// optional `next_step`.
//
// This is deliberately not `components/broker/operation-error.ts`. That module
// reads a different wire shape (`reason_code` / `remediation`) and derives
// remediation copy from an `OperationKind` × status table; this surface
// authors every word server-side and the client must not compose one. The one
// thing this module writes itself is the sentence for a response that carried
// no refusal at all — a transport failure or an untyped status — and that
// sentence is about the request, never about the configuration domain.

import { HttpErrorResponse } from '@angular/common/http';

/** The two reasons that mean "someone else changed this first" (contract §5). */
const STALE_WRITE_REASONS: ReadonlySet<string> = new Set([
  'revision_conflict',
  'selection_generation_conflict',
]);

export interface ConfigurationRefusal {
  /** The server's code-like reason, or `null` when it sent none. Render through `receiptLabel`. */
  readonly reason: string | null;
  /** Backend-authored prose. Rendered verbatim — never piped, never rewritten. */
  readonly message: string;
  /** Backend-authored next step when the server supplied one; several 422s do not. */
  readonly nextStep: string | null;
  /** HTTP status, or `null` when the request never reached the server. */
  readonly status: number | null;
  /**
   * A stale write: this tab held an `expected_revision` or an
   * `expected_selection_generation` that something else has already moved past.
   * Nothing was overwritten; the surface must reload rather than retry.
   */
  readonly stale: boolean;
}

/**
 * A refusal this surface can state without asking the server — the request was
 * never worth sending. It carries no `reason`, because a reason code is the
 * server's vocabulary and inventing one would put a token in a receipt that no
 * backend ever emitted.
 */
export function clientRefusal(message: string, nextStep: string | null): ConfigurationRefusal {
  return { reason: null, message, nextStep, status: null, stale: false };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readDetail(body: unknown): { reason: string; message: string; nextStep: string | null } | null {
  if (!isRecord(body) || !isRecord(body['detail'])) return null;
  const detail = body['detail'];
  const reason = detail['reason'];
  const message = detail['message'];
  const nextStep = detail['next_step'];
  if (typeof reason !== 'string' || typeof message !== 'string') return null;
  return { reason, message, nextStep: typeof nextStep === 'string' ? nextStep : null };
}

/**
 * Normalise a rejected configuration call into the refusal a surface renders.
 * A typed refusal keeps every server-authored word; anything else becomes an
 * honest statement that the request failed, with no invented domain reason.
 */
export function toConfigurationRefusal(error: unknown): ConfigurationRefusal {
  const status = error instanceof HttpErrorResponse && error.status !== 0 ? error.status : null;
  const detail = error instanceof HttpErrorResponse ? readDetail(error.error) : null;
  if (detail !== null) {
    return {
      reason: detail.reason,
      message: detail.message,
      nextStep: detail.nextStep,
      status,
      stale: STALE_WRITE_REASONS.has(detail.reason),
    };
  }
  return {
    reason: null,
    message:
      status === null
        ? 'The configuration service could not be reached, so nothing was changed.'
        : `The configuration service answered ${status} without a refusal reason, so it is not known whether anything changed.`,
    nextStep: 'Reload the configuration to read what the service currently holds.',
    status,
    stale: false,
  };
}
