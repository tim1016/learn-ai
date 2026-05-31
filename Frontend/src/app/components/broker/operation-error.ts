// Operation error model for broker operations (handoff: "excellent error
// messaging"). The wire stays strings-only (FastAPI `HTTPException(detail=...)`);
// the frontend derives a category + "what to do next" from the pair it already
// knows for free — the operation the user invoked × the HTTP status — and NEVER
// by parsing the backend `detail` string (robust to backend wording drift).
//
// Status-code semantics the backend uses (ADR 0004/0006):
//   400 validation · 404 not-found · 409 domain/precondition · 503 infra.

import { HttpErrorResponse } from '@angular/common/http';

export type OperationKind =
  | 'deploy'
  | 'start'
  | 'stop'
  | 'pause'
  | 'resume'
  | 'flatten'
  | 'reconcile'
  | 'mark-poisoned';

export type ErrorCategory =
  | 'validation'
  | 'not-found'
  | 'precondition'
  | 'infra'
  | 'unknown';

export interface OperationError {
  category: ErrorCategory;
  /** Short, human "what failed" — derived from (operation, status). */
  title: string;
  /** The backend's literal `detail` string. Rendered as-is, never parsed. */
  detail: string;
  /** "What to do next" — derived from (operation, status), not from `detail`. */
  remediation: string;
  /** HTTP status when known; null for a transport/connection failure. */
  status: number | null;
}

const CATEGORY_BY_STATUS: Record<number, ErrorCategory> = {
  400: 'validation',
  404: 'not-found',
  409: 'precondition',
  503: 'infra',
};

const OPERATION_LABEL: Record<OperationKind, string> = {
  deploy: 'Deploy',
  start: 'Start',
  stop: 'Stop',
  pause: 'Pause',
  resume: 'Resume',
  flatten: 'Flatten',
  reconcile: 'Reconcile',
  'mark-poisoned': 'Mark poisoned',
};

// Remediation keyed on (operation, status). Most-specific cell wins; a generic
// per-status fallback covers the rest. Backend `detail` is never consulted here.
const REMEDIATION: Partial<Record<OperationKind, Partial<Record<number, string>>>> = {
  deploy: {
    409: 'A run with these inputs already exists, or the working tree is dirty. Commit or stash the listed paths, then deploy again.',
    400: 'Check the strategy spec path and the QC audit-copy path exist and are committed.',
    503: 'The live engine is unavailable or git is unavailable. Start the live engine and retry.',
  },
  start: {
    409: 'A run is already active for this instance. Stop it before starting another.',
    404: 'No run directory was found for this instance. Deploy a run first.',
    503: 'The live engine is unavailable. Start the live engine and retry.',
  },
  stop: {
    404: 'No live process is bound to this instance — nothing to stop.',
    503: 'The live engine is unavailable. Start the live engine and retry.',
  },
  flatten: { 409: 'No live run is bound to this instance. Start the instance before issuing commands.' },
  reconcile: { 409: 'No live run is bound to this instance. Start the instance before issuing commands.' },
  'mark-poisoned': { 409: 'No live run is bound to this instance. Start the instance before issuing commands.' },
};

const GENERIC_REMEDIATION: Record<ErrorCategory, string> = {
  validation: 'Check the values you submitted and try again.',
  'not-found': 'The target no longer exists — refresh and try again.',
  precondition: 'A precondition is not met. Resolve the blocker shown below, then retry.',
  infra: 'A required service is unavailable. Check connectivity and retry.',
  unknown: 'The operation failed. Retry; if it persists, check the service logs.',
};

const TITLE_BY_CATEGORY: Record<ErrorCategory, string> = {
  validation: 'invalid request',
  'not-found': 'not found',
  precondition: 'blocked',
  infra: 'service unavailable',
  unknown: 'failed',
};

function categoryOf(status: number | null): ErrorCategory {
  if (status === null) return 'infra'; // connection/transport failure
  return CATEGORY_BY_STATUS[status] ?? 'unknown';
}

/**
 * Build a structured OperationError from the operation and HTTP status.
 * `detail` is shown verbatim; remediation comes from the (operation, status)
 * lookup, falling back to a per-category generic.
 */
export function describeOperationError(
  operation: OperationKind,
  status: number | null,
  detail: string,
): OperationError {
  const category = categoryOf(status);
  const remediation =
    (status !== null ? REMEDIATION[operation]?.[status] : undefined) ??
    GENERIC_REMEDIATION[category];
  return {
    category,
    title: `${OPERATION_LABEL[operation]} — ${TITLE_BY_CATEGORY[category]}`,
    detail,
    remediation,
    status,
  };
}

/**
 * Normalise an unknown thrown value (typically an Angular `HttpErrorResponse`)
 * into an OperationError. Reads the status and the FastAPI `{detail}` body; the
 * detail is the only thing taken from the wire, and only as the literal line.
 */
export function toOperationError(operation: OperationKind, err: unknown): OperationError {
  let status: number | null = null;
  let detail: string;
  if (err instanceof HttpErrorResponse) {
    // status 0 means the request never reached the server (connection refused,
    // CORS, offline) — treat as a transport/infra failure, not a real 0.
    status = err.status === 0 ? null : err.status;
    const body = err.error;
    if (typeof body === 'string') {
      detail = body;
    } else if (body && typeof body === 'object' && typeof (body as { detail?: unknown }).detail === 'string') {
      detail = (body as { detail: string }).detail;
    } else {
      detail = err.message;
    }
  } else if (err instanceof Error) {
    detail = err.message;
  } else {
    detail = String(err);
  }
  return describeOperationError(operation, status, detail);
}
