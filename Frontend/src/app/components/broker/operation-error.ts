// Operation error model for broker operations (handoff: "excellent error
// messaging"). Legacy FastAPI errors use string `detail`, while newer
// deterministic preconditions may use a structured `detail` object. For string
// details, the frontend derives category + remediation from operation × status
// and never parses backend wording. For structured contracts, it renders the
// server-authored message/remediation.
//
// Status-code semantics the backend uses (ADR 0004/0006):
//   400 validation · 404 not-found · 409 domain/precondition · 503 infra.

import { HttpErrorResponse } from '@angular/common/http';
import type {
  OperatorAction,
  OperatorBlocker,
  OperatorConditionScope,
  OperatorConfirmationCopy,
  OperatorHost,
  OperatorMove,
} from '../../api/operator-blocker.types';

export type OperationKind =
  | 'deploy'
  | 'start'
  | 'stop'
  | 'pause'
  | 'resume'
  | 'flatten'
  | 'reconcile'
  | 'mark-poisoned'
  | 'renew-lease'
  | 'recovery-override';

export type ErrorCategory =
  | 'validation'
  | 'not-found'
  | 'precondition'
  | 'infra'
  | 'unknown'
  // PRD #619-C5 — single-shot mutation reached the daemon but the
  // response was lost. Ambiguous: the mutation may or may not have
  // executed. Distinct category so the cockpit can flag "refresh state
  // before retrying" instead of the canned 409 remediation.
  | 'outcome-unknown';

export interface OperationError {
  category: ErrorCategory;
  /** Short, human "what failed" — derived from (operation, status). */
  title: string;
  /** Backend-authored detail text, either from string detail or structured message. */
  detail: string;
  /** "What to do next" from operation/status or a structured server contract. */
  remediation: string;
  /** HTTP status when known; null for a transport/connection failure. */
  status: number | null;
  /** Server-owned reason token when the response used the typed precondition contract. */
  reason_code?: string;
  /** Server gate that rejected the operation, when supplied. */
  gate_id?: string;
  /** Exact server blockers supplied with a typed launch rejection. */
  blockers?: readonly OperatorBlocker[];
  mutation_attempt_id?: string;
  mutation_dispatch_state?: 'OUTCOME_UNKNOWN';
}

const OUTCOME_UNKNOWN_ENDPOINTS = [
  'deploy',
  'start_run',
  'stop_run',
  'end_day_now',
  'emergency_flatten',
  'renew_daemon_lease',
] as const;

export type OutcomeUnknownEndpoint = (typeof OUTCOME_UNKNOWN_ENDPOINTS)[number];

const OUTCOME_UNKNOWN_ENDPOINT_SET: ReadonlySet<string> = new Set(OUTCOME_UNKNOWN_ENDPOINTS);

/** Structured 409 body for ambiguous-outcome mutations (PRD #619-C5). */
export interface OutcomeUnknownBody {
  outcome: 'UNKNOWN';
  reason_code: 'OUTCOME_UNKNOWN';
  error_category: string;
  detail: string | null;
  endpoint: OutcomeUnknownEndpoint;
  occurred_at_ms: number;
  runbook_hint: string;
  mutation_attempt_id?: string;
  mutation_dispatch_state?: 'OUTCOME_UNKNOWN';
}

/** Structured 409 body for deterministic domain/precondition blocks. */
export interface PreconditionBody {
  reason_code: string;
  message: string;
  remediation?: string;
  gate_id?: string;
  blockers?: readonly OperatorBlocker[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isOneOf<T extends string>(value: unknown, allowed: readonly T[]): value is T {
  return typeof value === 'string' && allowed.includes(value as T);
}

function readConfirmation(value: unknown): OperatorConfirmationCopy | null {
  if (!isRecord(value)) return null;
  const title = value['title'];
  const body = value['body'];
  const consequence = value['consequence'];
  const confirmLabel = value['confirm_label'];
  const requiredToken = value['required_token'];
  if (
    typeof title !== 'string' ||
    typeof body !== 'string' ||
    typeof consequence !== 'string' ||
    typeof confirmLabel !== 'string' ||
    typeof requiredToken !== 'string'
  ) {
    return null;
  }
  return {
    title,
    body,
    consequence,
    confirm_label: confirmLabel,
    required_token: requiredToken,
  };
}

function readOperatorAction(value: unknown): OperatorAction | null {
  if (!isRecord(value)) return null;
  const kind = value['kind'];
  switch (kind) {
    case 'navigate': {
      const route = value['route'];
      const fragment = value['fragment'];
      return typeof route === 'string' && (typeof fragment === 'string' || fragment === null)
        ? { kind, route, fragment }
        : null;
    }
    case 'confirm_in_form': {
      const anchor = value['anchor'];
      return typeof anchor === 'string' ? { kind, anchor } : null;
    }
    case 'open_runbook': {
      const slug = value['slug'];
      return typeof slug === 'string' ? { kind, slug } : null;
    }
    case 'retire_replace':
    case 'remove':
      return { kind };
    default:
      return null;
  }
}

function readOperatorMove(value: unknown): OperatorMove | null {
  if (!isRecord(value)) return null;
  const label = value['label'];
  const target = value['target'];
  const action = readOperatorAction(value['action']);
  if (typeof label !== 'string' || (typeof target !== 'string' && target !== null) || action === null) {
    return null;
  }
  const rawConfirmation = value['confirmation'];
  if (rawConfirmation !== undefined && rawConfirmation !== null) {
    const confirmation = readConfirmation(rawConfirmation);
    if (confirmation === null) return null;
    return { label, action, target, confirmation };
  }
  return { label, action, target };
}

function readOperatorBlocker(value: unknown): OperatorBlocker | null {
  if (!isRecord(value) || !isRecord(value['condition'])) return null;
  const condition = value['condition'];
  const id = condition['id'];
  const severity = condition['severity'];
  const scope = condition['scope'];
  const evidence = condition['evidence'];
  const host = value['host'];
  const disposition = value['disposition'];
  const headline = value['headline'];
  const detail = value['detail'];
  const primaryMove = value['primary_move'];
  const secondaryMoves = value['secondary_moves'];
  const appliesTo = value['applies_to'];
  if (
    typeof id !== 'string' ||
    !isOneOf(severity, ['blocking', 'warning'] as const) ||
    !isOneOf(scope, ['bot', 'account', 'broker', 'fleet', 'host', 'strategy'] as const) ||
    !isRecord(evidence) ||
    !isOneOf(host, ['bot_cockpit', 'deploy_preflight', 'fleet_roster', 'account_monitor'] as const) ||
    !isOneOf(disposition, ['fix_here', 'fix_elsewhere', 'wait', 'terminal'] as const) ||
    typeof headline !== 'string' ||
    (typeof detail !== 'string' && detail !== null) ||
    !Array.isArray(secondaryMoves) ||
    !isOneOf(appliesTo, ['deploy', 'run', 'both'] as const)
  ) {
    return null;
  }
  const parsedPrimaryMove = primaryMove === null ? null : readOperatorMove(primaryMove);
  const parsedSecondaryMoves = secondaryMoves.map(readOperatorMove);
  if (parsedPrimaryMove === null && primaryMove !== null) return null;
  if (parsedSecondaryMoves.some((move) => move === null)) return null;
  if (!Object.values(evidence).every((entry) =>
    entry === null || typeof entry === 'string' || typeof entry === 'number' || typeof entry === 'boolean',
  )) {
    return null;
  }
  return {
    condition: {
      id,
      severity,
      scope: scope as OperatorConditionScope,
      evidence: evidence as Record<string, string | number | boolean | null>,
    },
    host: host as OperatorHost,
    disposition,
    headline,
    detail,
    primary_move: parsedPrimaryMove,
    secondary_moves: parsedSecondaryMoves as OperatorMove[],
    applies_to: appliesTo,
  };
}

function readOperatorBlockers(value: unknown): readonly OperatorBlocker[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) return undefined;
  const blockers = value.map(readOperatorBlocker);
  return blockers.every((blocker) => blocker !== null)
    ? blockers as OperatorBlocker[]
    : undefined;
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
  'renew-lease': 'Renew lease',
  'recovery-override': 'Recovery override',
};

// Remediation keyed on (operation, status). Most-specific cell wins; a generic
// per-status fallback covers the rest. Legacy string `detail` is never parsed.
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
  'renew-lease': {
    409: 'Refresh Bot Control to read the current daemon lease before retrying.',
    503: 'The host daemon is unavailable. Check the local daemon and retry.',
  },
  'recovery-override': {
    409: 'Refresh Bot Control. The blocker may already have changed.',
    404: 'Deploy a run for this bot before recording recovery evidence.',
  },
};

const GENERIC_REMEDIATION: Record<ErrorCategory, string> = {
  validation: 'Check the values you submitted and try again.',
  'not-found': 'The target no longer exists — refresh and try again.',
  precondition: 'A precondition is not met. Resolve the conflict and retry.',
  infra: 'A required service is unavailable. Check connectivity and retry.',
  unknown: 'The operation failed. Retry; if it persists, check the service logs.',
  'outcome-unknown':
    'The request reached the host daemon but no confirmation came back. ' +
    'Refresh the cockpit to read live state before deciding whether to retry.',
};

const TITLE_BY_CATEGORY: Record<ErrorCategory, string> = {
  validation: 'invalid request',
  'not-found': 'not found',
  precondition: 'blocked',
  infra: 'service unavailable',
  unknown: 'failed',
  'outcome-unknown': 'outcome unknown',
};

function categoryOf(status: number | null): ErrorCategory {
  if (status === null) return 'infra'; // connection/transport failure
  return CATEGORY_BY_STATUS[status] ?? 'unknown';
}

function typedBodyRemediationFallback(operation: OperationKind, status: number | null): string {
  const category = categoryOf(status);
  // A typed 409 describes a server-specific gate, so legacy operation advice
  // can be misleading. A typed 503 can likewise describe an unavailable
  // dependency other than the live engine. Prefer the server remediation when
  // present and otherwise retain a truthful category-level fallback.
  if (status === 409 || status === 503) return GENERIC_REMEDIATION[category];
  return (status !== null ? REMEDIATION[operation]?.[status] : undefined) ?? GENERIC_REMEDIATION[category];
}

function isOutcomeUnknownEndpoint(value: unknown): value is OutcomeUnknownEndpoint {
  return typeof value === 'string' && OUTCOME_UNKNOWN_ENDPOINT_SET.has(value);
}

/**
 * Build a structured OperationError from the operation and HTTP status.
 * `detail` is shown verbatim; remediation comes from the (operation, status)
 * lookup, falling back to a per-category generic. When the backend ships a
 * server-authored remediation (e.g. PRD #619-C5's `runbook_hint`), pass it
 * via `remediationOverride` and the canned lookup is skipped.
 */
export function describeOperationError(
  operation: OperationKind,
  status: number | null,
  detail: string,
  options: {
    category?: ErrorCategory;
    remediationOverride?: string;
    reasonCode?: string;
    gateId?: string;
    blockers?: readonly OperatorBlocker[];
    mutationAttemptId?: string;
    mutationDispatchState?: 'OUTCOME_UNKNOWN';
  } = {},
): OperationError {
  const category = options.category ?? categoryOf(status);
  const remediation =
    options.remediationOverride ??
    (status !== null ? REMEDIATION[operation]?.[status] : undefined) ??
    GENERIC_REMEDIATION[category];
  return {
    category,
    title: `${OPERATION_LABEL[operation]} ${TITLE_BY_CATEGORY[category]}`,
    detail,
    remediation,
    status,
    ...(options.reasonCode ? { reason_code: options.reasonCode } : {}),
    ...(options.gateId ? { gate_id: options.gateId } : {}),
    ...(options.blockers?.length ? { blockers: options.blockers } : {}),
    ...(options.mutationAttemptId
      ? {
          mutation_attempt_id: options.mutationAttemptId,
          mutation_dispatch_state: options.mutationDispatchState,
        }
      : {}),
  };
}

/**
 * Recognise the structured 409 body PRD #619-C5 ships for ambiguous-outcome
 * mutations. Returns the parsed object when the shape matches exactly, else
 * `null`. The cockpit needs this because the structured object is nested
 * under FastAPI's ``HTTPException.detail`` and the legacy parser only read
 * `detail` as a string.
 */
export function readOutcomeUnknownBody(body: unknown): OutcomeUnknownBody | null {
  if (!body || typeof body !== 'object') return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== 'object') return null;
  const d = detail as Record<string, unknown>;
  const endpoint = d['endpoint'];
  const errorCategory = d['error_category'];
  const occurredAtMs = d['occurred_at_ms'];
  const runbookHint = d['runbook_hint'];
  const bodyDetail = d['detail'];
  const mutationAttemptId = d['mutation_attempt_id'];
  const mutationDispatchState = d['mutation_dispatch_state'];
  if (
    d['outcome'] !== 'UNKNOWN' ||
    d['reason_code'] !== 'OUTCOME_UNKNOWN' ||
    typeof errorCategory !== 'string' ||
    !isOutcomeUnknownEndpoint(endpoint) ||
    typeof occurredAtMs !== 'number' ||
    typeof runbookHint !== 'string'
  ) {
    return null;
  }
  return {
    outcome: 'UNKNOWN',
    reason_code: 'OUTCOME_UNKNOWN',
    error_category: errorCategory,
    detail: typeof bodyDetail === 'string' ? bodyDetail : null,
    endpoint,
    occurred_at_ms: occurredAtMs,
    runbook_hint: runbookHint,
    ...(typeof mutationAttemptId === 'string' && mutationDispatchState === 'OUTCOME_UNKNOWN'
      ? {
          mutation_attempt_id: mutationAttemptId,
          mutation_dispatch_state: mutationDispatchState,
        }
      : {}),
  };
}

/**
 * Recognise structured deterministic server-error bodies such as:
 * ``{detail: {reason_code, message, remediation?, gate_id?}}``.
 * Unlike the legacy string-detail path, this shape is an explicit server
 * contract, so the UI may render the server-authored remediation.
 */
export function readPreconditionBody(body: unknown): PreconditionBody | null {
  if (!body || typeof body !== 'object') return null;
  const detail = (body as { detail?: unknown }).detail;
  if (!detail || typeof detail !== 'object') return null;
  const d = detail as Record<string, unknown>;
  const reasonCode = d['reason_code'];
  const message = d['message'];
  const remediation = d['remediation'];
  const gateId = d['gate_id'];
  const blockers = readOperatorBlockers(d['blockers']);
  if (typeof reasonCode !== 'string' || typeof message !== 'string') {
    return null;
  }
  return {
    reason_code: reasonCode,
    message,
    remediation: typeof remediation === 'string' ? remediation : undefined,
    gate_id: typeof gateId === 'string' ? gateId : undefined,
    ...(blockers !== undefined ? { blockers } : {}),
  };
}

/**
 * Normalise an unknown thrown value (typically an Angular `HttpErrorResponse`)
 * into an OperationError. Reads the status and the FastAPI `{detail}` body; the
 * detail is the only thing taken from the wire, and only as the literal line.
 *
 * Special-cases PRD #619-C5's structured 409 body — the canned 409 remediation
 * is replaced by the server-authored `runbook_hint` and the category is
 * promoted to ``outcome-unknown`` so the cockpit can flag the ambiguous state
 * distinctly from "a precondition isn't met".
 */
export function toOperationError(operation: OperationKind, err: unknown): OperationError {
  let status: number | null = null;
  let detail: string;
  let outcomeUnknown: OutcomeUnknownBody | null = null;
  let precondition: PreconditionBody | null = null;
  if (err instanceof HttpErrorResponse) {
    // status 0 means the request never reached the server (connection refused,
    // CORS, offline) — treat as a transport/infra failure, not a real 0.
    status = err.status === 0 ? null : err.status;
    const body = err.error;
    outcomeUnknown = readOutcomeUnknownBody(body);
    if (outcomeUnknown !== null) {
      detail =
        outcomeUnknown.detail ?? `Daemon transport failed: ${outcomeUnknown.error_category}.`;
    } else if (typeof body === 'string') {
      detail = body;
    } else if (body && typeof body === 'object' && typeof (body as { detail?: unknown }).detail === 'string') {
      detail = (body as { detail: string }).detail;
    } else if (body && typeof body === 'object') {
      precondition = readPreconditionBody(body);
      if (precondition !== null) {
        detail = precondition.message;
      } else {
        const nested = (body as { detail?: unknown }).detail;
        detail = nested && typeof nested === 'object' && typeof (nested as { message?: unknown }).message === 'string'
          ? (nested as { message: string }).message
          : err.message;
      }
    } else {
      detail = err.message;
    }
  } else if (err instanceof Error) {
    detail = err.message;
  } else {
    detail = String(err);
  }
  if (outcomeUnknown !== null) {
    return describeOperationError(operation, status, detail, {
      category: 'outcome-unknown',
      remediationOverride: outcomeUnknown.runbook_hint,
      reasonCode: outcomeUnknown.reason_code,
      mutationAttemptId: outcomeUnknown.mutation_attempt_id,
      mutationDispatchState: outcomeUnknown.mutation_dispatch_state,
    });
  }
  if (precondition !== null) {
    return describeOperationError(operation, status, detail, {
      // A typed precondition is a server contract, but not every endpoint
      // supplies an explicit remediation. Do not substitute the legacy
      // deploy-409 advice (which mentions working trees) for a different
      // gate such as a broker or validation blocker. Typed 404/400 contracts
      // still use their precise operation guidance when the server omits it.
      remediationOverride: precondition.remediation ?? typedBodyRemediationFallback(operation, status),
      reasonCode: precondition.reason_code,
      gateId: precondition.gate_id,
      blockers: precondition.blockers,
    });
  }
  return describeOperationError(operation, status, detail);
}
