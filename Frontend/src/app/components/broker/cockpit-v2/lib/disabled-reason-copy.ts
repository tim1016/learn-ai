// Operator-language copy for every server-authored disabled-reason
// code on the operator_surface action capabilities.
//
// The closed reason-code vocabulary lives on the server (see
// ``PythonDataService/app/services/operator_capability.py`` →
// ``REASON_CODES``, which unions the per-action codes with
// ``PythonDataService/app/services/resume_guard_state.py`` →
// ``RESUME_REASON_CODES``).  ADR-0010 §A3 fixes the priority order
// the *resolver* sorts the codes into; the cockpit's job is to take
// the head reason (and the full list when displayed expanded) and
// render the operator-language equivalent without inventing operational
// truth.
//
// ADR-0013 §4 — "Presentation copy lookup keyed on a closed
// server-authored enum" is explicitly permitted Frontend-side.  This
// file is the canonical map.  Adding a new code on the server requires
// adding the matching copy here; the
// ``disabled-reason-copy.spec.ts`` parity test pins the union.

/** Strongly-typed union of every closed server-authored reason code
 *  the cockpit can encounter on ``ActionCapability.disabled_reasons``
 *  or ``ActionCapability.disabled_reason_code``.  Imported from the
 *  server side via the operator-runbook source-of-truth comment above —
 *  Python is authoritative; this list mirrors it under spec lock. */
export type OperatorReasonCode =
  // Action-conflict matrix (PRD #619-D2)
  | 'MUTATION_UNRESOLVED_START'
  | 'MUTATION_UNRESOLVED_STOP'
  | 'MUTATION_UNRESOLVED_FLATTEN'
  | 'MUTATION_UNRESOLVED_RESUME'
  // Single-shot mutation surface (PRD #619-C5)
  | 'OUTCOME_UNKNOWN'
  // Live-binding / live-effect gates
  | 'NO_LIVE_BINDING'
  | 'NO_OWNED_POSITIONS'
  | 'ALREADY_POISONED'
  | 'ALREADY_STOPPED'
  // Runtime freshness (PRD #619-B7)
  | 'POSTURE_DEMOTED'
  // Broker safety identity (ADR-0011)
  | 'BROKER_SAFETY_UNSAFE'
  | 'BROKER_SAFETY_UNKNOWN'
  // Submission capability (ADR-0011 amendment, PRD #619-A)
  | 'SUBMISSION_CAPABILITY_BLOCKED'
  | 'SUBMISSION_CAPABILITY_UNKNOWN'
  // Reconciliation receipt gate (PRD #616)
  | 'RECONCILIATION_FAILED'
  | 'RECONCILIATION_STALE'
  | 'RECONCILIATION_NOT_AVAILABLE'
  | 'RECONCILIATION_UNKNOWN'
  // Uncertain-intent gate (ADR-0008)
  | 'UNRESOLVED_UNCERTAIN_INTENT'
  | 'UNCERTAIN_INTENT_STATE_UNKNOWN'
  // Intent-state pair rules (ADR-0010 §A5)
  | 'ALREADY_RUNNING'
  | 'ALREADY_PAUSED'
  | 'STOPPED_REQUIRES_REDEPLOY'
  | 'REDEPLOY_REQUIRED';

/** Frontend-only transient codes that are NOT on the server vocabulary
 *  but appear in tooltips for local-only conditions (control-plane
 *  transport degraded, request in flight).  They are clearly named
 *  ``LOCAL_*`` so a misclassification cannot pretend to be server
 *  authority (run-prompt §9.5). */
export type LocalReasonCode = 'LOCAL_TRANSPORT_STALE' | 'LOCAL_REQUEST_IN_FLIGHT';

const OPERATOR_REASON_COPY: Record<OperatorReasonCode, string> = {
  // Action-conflict matrix
  MUTATION_UNRESOLVED_START:
    "A prior Start attempt hasn't been proven to complete. Use Reconcile on the Audit tab before retrying.",
  MUTATION_UNRESOLVED_STOP:
    "A prior Stop attempt hasn't been proven to complete. Use Reconcile on the Audit tab before retrying.",
  MUTATION_UNRESOLVED_FLATTEN:
    "A prior Flatten-and-pause attempt hasn't been proven to complete. Use Reconcile on the Audit tab before retrying.",
  MUTATION_UNRESOLVED_RESUME:
    "A prior Resume attempt hasn't been proven to complete. Use Reconcile on the Audit tab before retrying.",
  // Mutation transport
  OUTCOME_UNKNOWN:
    'The mutation transport returned an ambiguous outcome. Use Reconcile on the Audit tab to classify the prior attempt.',
  // Live-binding / live-effect gates
  NO_LIVE_BINDING:
    'No live binding — the host runner is not bound to this instance. Start a runner first.',
  NO_OWNED_POSITIONS: 'Nothing to flatten — the broker reports no owned positions for this bot.',
  ALREADY_POISONED: 'This run is already marked POISONED. Redeploy to recover.',
  ALREADY_STOPPED: 'Already STOPPED. STOPPED is a terminal state; revival requires Redeploy.',
  // Runtime freshness
  POSTURE_DEMOTED:
    'Runtime evidence is stale (control-plane lease or daemon heartbeat). Resume/Flatten are held until fresh evidence returns.',
  // Broker safety identity
  BROKER_SAFETY_UNSAFE:
    'Broker safety verdict is UNSAFE — non-paper signals detected. Resume is disabled by the server until the verdict returns to paper-only.',
  BROKER_SAFETY_UNKNOWN:
    'Broker safety verdict is UNKNOWN — not enough signal to confirm paper. Resume is disabled by the server until paper-only is confirmed.',
  // Submission capability
  SUBMISSION_CAPABILITY_BLOCKED:
    "Declared submit mode and the run's readonly setting do not satisfy the run contract. Inspect the run sidecar and redeploy if needed.",
  SUBMISSION_CAPABILITY_UNKNOWN:
    'Submission capability cannot be proven from durable child/run evidence. Resume is held until the sidecar reports a known state.',
  // Reconciliation receipt
  RECONCILIATION_FAILED:
    'The last reconciliation receipt reports a divergence. Reconcile manually before Resume.',
  RECONCILIATION_STALE:
    'The last reconciliation receipt predates the current run/broker state. Reconcile again before Resume.',
  RECONCILIATION_NOT_AVAILABLE:
    'No reconciliation receipt is available yet (the writer is not wired downstream). Treat as informational.',
  RECONCILIATION_UNKNOWN:
    'The reconciliation receipt is unreadable or malformed. Resume is held until a clean receipt is available.',
  // Uncertain-intent
  UNRESOLVED_UNCERTAIN_INTENT:
    'An uncertain submit intent is unresolved in the WAL. Use Reconcile or operator-command flow to clear it before Resume.',
  UNCERTAIN_INTENT_STATE_UNKNOWN:
    'Uncertain-intent state cannot be read. Resume is held until the WAL is observable.',
  // Intent-state pair rules
  ALREADY_RUNNING: 'Bot is already RUNNING.',
  ALREADY_PAUSED: 'Bot is already PAUSED.',
  STOPPED_REQUIRES_REDEPLOY:
    'Bot is STOPPED. Resume from STOPPED is a Redeploy, not a desired-state write.',
  REDEPLOY_REQUIRED: 'This run is dead (poisoned). Redeploy to recover.',
};

const LOCAL_REASON_COPY: Record<LocalReasonCode, string> = {
  LOCAL_TRANSPORT_STALE:
    'Cockpit control-plane transport is not currently CONNECTED — refusing local dispatch until it recovers.',
  LOCAL_REQUEST_IN_FLIGHT:
    'A previous request for this instance is still pending. Wait for it to settle.',
};

/** Resolve a single reason code (server or local) to operator-language
 *  copy. Unknown codes are returned verbatim so they remain visibly
 *  diagnosable per run-prompt §9.4 (no silent generic-success copy). */
export function disabledReasonCopy(code: string | null | undefined): string | null {
  if (code === null || code === undefined || code === '') return null;
  if (code in OPERATOR_REASON_COPY) {
    return OPERATOR_REASON_COPY[code as OperatorReasonCode];
  }
  if (code in LOCAL_REASON_COPY) {
    return LOCAL_REASON_COPY[code as LocalReasonCode];
  }
  // Unknown / server-introduced code we don't have copy for yet. Keep
  // the raw code visible so the operator can search the runbook and
  // the regression is immediately catchable.
  return `Unrecognized reason code: ${code}`;
}

/** Compose a tooltip for an action button.  Priority:
 *  1. transport-stale (local fail-closed gate)
 *  2. server-authored ``disabled_reason_code`` (head of priority order)
 *  3. fallback action label so an enabled button still has a hover hint
 */
export function actionTooltip(args: {
  enabled: boolean;
  serverReasonCode: string | null;
  localTransportStale: boolean;
  busy: boolean;
  fallbackLabel: string;
}): string {
  if (args.localTransportStale) {
    return disabledReasonCopy('LOCAL_TRANSPORT_STALE') ?? args.fallbackLabel;
  }
  if (args.busy) {
    return disabledReasonCopy('LOCAL_REQUEST_IN_FLIGHT') ?? args.fallbackLabel;
  }
  if (!args.enabled && args.serverReasonCode) {
    return disabledReasonCopy(args.serverReasonCode) ?? args.fallbackLabel;
  }
  return args.fallbackLabel;
}

/** Convenience for templates needing the closed union — exposed for
 *  spec lock against the Python ``REASON_CODES`` set. */
export const ALL_OPERATOR_REASON_CODES: readonly OperatorReasonCode[] = Object.keys(
  OPERATOR_REASON_COPY,
) as OperatorReasonCode[];
export const ALL_LOCAL_REASON_CODES: readonly LocalReasonCode[] = Object.keys(
  LOCAL_REASON_COPY,
) as LocalReasonCode[];
