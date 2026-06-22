// PRD #607 + PRD #616 — test helper for the revised operator_surface
// contract (cockpit revision 2026-06-21 + 2026-06-20).
//
// Extended in PRD #616 with the new shapes:
//   - five canonical actions (added `stop`)
//   - `disabled_reasons` priority-ordered list on every ActionCapability
//   - `readiness_gates` projection with `GateSuggestedAction` closed union
//   - `FleetAccountSummary` (account identity + contamination)
//   - `LiveInstanceSummary` readiness_verdict + readiness_as_of_ms

import type {
  ActionCapability,
  FleetAccountSummary,
  FleetContamination,
  GateSuggestedAction,
  LiveInstanceSummary,
  OperatorGate,
  OperatorSurface,
} from '../app/api/live-instances.types';

const _capability = (
  enabled: boolean,
  effect: ActionCapability['effect'],
  code: string | null = null,
  reasons: string[] = [],
): ActionCapability => ({
  enabled,
  effect,
  disabled_reason_code: code,
  disabled_reasons: reasons,
});

/**
 * A benign all-defaults projection useful for fixtures that don't care
 * about cockpit verdicts.  Resume / Pause enabled as durable-only
 * writes; flatten-and-pause / stop / mark-poisoned disabled with
 * NO_LIVE_BINDING (unbound default).  Trading session is UNKNOWN so
 * tests opt in to a specific phase.
 */
export const DEFAULT_OPERATOR_SURFACE: OperatorSurface = {
  schema_version: 1,
  host_process: { state: 'IDLE', notice: null, copyable_command: null },
  prior_run: { classification: 'UNKNOWN' },
  broker: { safety_verdict: 'UNKNOWN', connection: 'UNKNOWN' },
  configuration: { verdict: 'UNKNOWN', reason_codes: [] },
  current_risk: {
    posture: 'UNKNOWN',
    pending_order_count: null,
    verdict: 'UNKNOWN',
    unrealized_pnl: null,
  },
  daily_order_cap: { used: null, limit: null },
  action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
  actions: {
    resume: _capability(true, 'DURABLE_ONLY'),
    pause: _capability(true, 'DURABLE_ONLY'),
    stop: _capability(true, 'DURABLE_ONLY'),
    flatten_and_pause: _capability(false, 'LIVE_ACTUATION', 'NO_LIVE_BINDING', [
      'NO_LIVE_BINDING',
    ]),
    mark_poisoned: _capability(false, 'LIVE_ACTUATION', 'NO_LIVE_BINDING', [
      'NO_LIVE_BINDING',
    ]),
  },
  trading_session: {
    phase: 'UNKNOWN',
    permits_strategy_activity: null,
    next_transition_ms: null,
    timezone: 'America/New_York',
    as_of_ms: 0,
  },
  readiness_gates: [],
  runtime_freshness: null,
  control_plane: null,
};

// ---------------------------------------------------------------------------
// PRD #616 — guarded-Resume capability fixtures
// ---------------------------------------------------------------------------

export const RESUME_DISABLED_BROKER_UNSAFE: ActionCapability = _capability(
  false,
  'DURABLE_ONLY',
  'BROKER_SAFETY_UNSAFE',
  ['BROKER_SAFETY_UNSAFE'],
);

export const RESUME_DISABLED_UNCERTAIN_INTENT: ActionCapability = _capability(
  false,
  'DURABLE_ONLY',
  'UNRESOLVED_UNCERTAIN_INTENT',
  ['UNRESOLVED_UNCERTAIN_INTENT'],
);

export const RESUME_DISABLED_RECONCILIATION_NA: ActionCapability = _capability(
  false,
  'DURABLE_ONLY',
  'RECONCILIATION_NOT_AVAILABLE',
  ['RECONCILIATION_NOT_AVAILABLE'],
);

export const RESUME_DISABLED_MULTI_REASON: ActionCapability = _capability(
  false,
  'DURABLE_ONLY',
  'BROKER_SAFETY_UNSAFE',
  ['BROKER_SAFETY_UNSAFE', 'UNRESOLVED_UNCERTAIN_INTENT', 'RECONCILIATION_FAILED'],
);

export const RESUME_DISABLED_ALREADY_RUNNING: ActionCapability = _capability(
  false,
  'LIVE_ACTUATION',
  'ALREADY_RUNNING',
  ['ALREADY_RUNNING'],
);

export const RESUME_DISABLED_STOPPED_REQUIRES_REDEPLOY: ActionCapability = _capability(
  false,
  'DURABLE_ONLY',
  'STOPPED_REQUIRES_REDEPLOY',
  ['STOPPED_REQUIRES_REDEPLOY'],
);

export const PAUSE_DISABLED_ALREADY_PAUSED: ActionCapability = _capability(
  false,
  'DURABLE_ONLY',
  'ALREADY_PAUSED',
  ['ALREADY_PAUSED'],
);

// ---------------------------------------------------------------------------
// PRD #616 — GateSuggestedAction union variants
// ---------------------------------------------------------------------------

export const SUGGESTED_RESUME: GateSuggestedAction = {
  kind: 'invoke_capability',
  capability: 'resume',
};

export const SUGGESTED_FOCUS_FLATTEN: GateSuggestedAction = {
  kind: 'focus_action',
  tab: 'status',
  action: 'flatten_and_pause',
};

export const SUGGESTED_FOCUS_MARK_POISONED: GateSuggestedAction = {
  kind: 'focus_action',
  tab: 'audit',
  action: 'mark_poisoned',
};

export const SUGGESTED_REDEPLOY: GateSuggestedAction = {
  kind: 'redeploy',
};

export const SUGGESTED_RUNBOOK: GateSuggestedAction = {
  kind: 'open_runbook',
  slug: 'broker-reconnect',
};

export const OPERATOR_GATE_PASSING: OperatorGate = {
  name: 'broker_connection',
  status: 'pass',
  severity: 'hard',
  detail: 'connected',
  suggested_action: null,
  suggested_action_unavailable_reason: 'GATE_PASSING',
};

export const OPERATOR_GATE_FAILING_REDEPLOY: OperatorGate = {
  name: 'poison_sentinel',
  status: 'fail',
  severity: 'hard',
  detail: 'poisoned.flag present',
  suggested_action: SUGGESTED_REDEPLOY,
  suggested_action_unavailable_reason: null,
};

export const OPERATOR_GATE_FAILING_NO_INLINE_REMEDIATION: OperatorGate = {
  name: 'daily_order_cap',
  status: 'fail',
  severity: 'hard',
  detail: '50 / 50 orders used',
  suggested_action: null,
  suggested_action_unavailable_reason: 'NO_INLINE_REMEDIATION',
};

export const OPERATOR_GATE_UNKNOWN_NAME: OperatorGate = {
  name: 'totally_invented_gate',
  status: 'fail',
  severity: 'hard',
  detail: '',
  suggested_action: null,
  suggested_action_unavailable_reason: 'UNKNOWN_GATE_NAME',
};

// ---------------------------------------------------------------------------
// PRD #616 — FleetAccountSummary fixtures
// ---------------------------------------------------------------------------

const _contamination_clean: FleetContamination = {
  net_positions: {},
  explained_total: {},
  explained_by_instance: [],
  residual: {},
  verdict: 'clean',
  policy_blocks_starts: false,
  summary: 'Account clean — every position is explained by a managed instance.',
};

const _contamination_dirty: FleetContamination = {
  net_positions: { SPY: 1 },
  explained_total: {},
  explained_by_instance: [],
  residual: { SPY: 1 },
  verdict: 'contaminated',
  policy_blocks_starts: false,
  summary: 'Account residual: SPY +1 unattributed outside managed namespaces.',
};

export const FLEET_ACCOUNT_CONSISTENT_CLEAN: FleetAccountSummary = {
  account_id: 'DU284968',
  account_identity: 'CONSISTENT',
  account_identity_reason_codes: [],
  contamination: _contamination_clean,
};

export const FLEET_ACCOUNT_CONFLICTING_CLEAN: FleetAccountSummary = {
  account_id: 'DU284968',
  account_identity: 'CONFLICTING',
  account_identity_reason_codes: ['INSTANCE_ACCOUNT_MISMATCH'],
  contamination: _contamination_clean,
};

export const FLEET_ACCOUNT_CONSISTENT_DIRTY: FleetAccountSummary = {
  account_id: 'DU284968',
  account_identity: 'CONSISTENT',
  account_identity_reason_codes: [],
  contamination: _contamination_dirty,
};

export const FLEET_ACCOUNT_UNKNOWN: FleetAccountSummary = {
  account_id: null,
  account_identity: 'UNKNOWN',
  account_identity_reason_codes: ['BROKER_ACCOUNT_UNAVAILABLE'],
  contamination: {
    ..._contamination_clean,
    verdict: 'unknown',
    summary: 'Net account position unavailable — contamination unknown.',
  },
};

// ---------------------------------------------------------------------------
// PRD #616 — LiveInstanceSummary fixtures (with readiness_verdict)
// ---------------------------------------------------------------------------

export const LIVE_INSTANCE_SUMMARY_READY: LiveInstanceSummary = {
  strategy_instance_id: 'dep_val_smoke_001',
  process_state: 'running',
  bound_run_id: 'run-1',
  latest_run_id: 'run-1',
  desired_state: 'RUNNING',
  readiness_verdict: 'READY',
  readiness_as_of_ms: 1_700_000_000_000,
};

export const LIVE_INSTANCE_SUMMARY_BLOCKED: LiveInstanceSummary = {
  strategy_instance_id: 'dep_val_smoke_002',
  process_state: 'idle',
  bound_run_id: null,
  latest_run_id: 'run-x',
  desired_state: 'PAUSED',
  readiness_verdict: 'BLOCKED',
  readiness_as_of_ms: 1_700_000_000_000,
};
