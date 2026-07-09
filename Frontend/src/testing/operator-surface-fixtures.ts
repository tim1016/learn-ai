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
  GateResult,
  GateSuggestedAction,
  LiveInstanceSummary,
  OperatorGate,
  OperatorNotice,
  OperatorSurfaceProofLine,
  OperatorSurface,
  OperatorSurfaceRuntimeFreshness,
} from '../app/api/live-instances.types';

const _gateResult = (
  gate_id: string,
  status: GateResult['status'],
  operator_reason: string,
  operator_next_step: string | null,
): GateResult => ({
  gate_id,
  status,
  source: 'fixture',
  operator_reason,
  operator_next_step,
  evidence_at_ms: 0,
});

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
  gate_results: [
    _gateResult(
      `action.${effect.toLowerCase()}`,
      enabled ? 'pass' : 'block',
      code ?? 'GATE_PASSING',
      code ?? 'GATE_PASSING',
    ),
  ],
});

const DEFAULT_PROOF_LINES: OperatorSurfaceProofLine[] = [
  {
    id: 'broker-proof',
    label: 'Broker',
    message: 'Broker proof is not available yet.',
    detail: 'Account safety proof is not recorded. Broker connection has not been proven.',
    tone: 'attention',
  },
  {
    id: 'submit-readiness',
    label: 'Trade submit',
    message: 'Safe to monitor',
    detail:
      'The cockpit can observe this bot, but order submission is not currently active or appropriate. 1 blocking proof still needs attention.',
    tone: 'attention',
  },
  {
    id: 'account-owner',
    label: 'Account owner',
    message: 'Waiting for AccountOwner proof.',
    detail: 'No AccountOwner artifact is available for this bot.',
    tone: 'attention',
  },
  {
    id: 'reconciliation',
    label: 'Reconciliation',
    message: 'Waiting for reconciliation proof.',
    detail: 'No reconciliation claim has been produced for this run.',
    tone: 'attention',
  },
  {
    id: 'runtime-freshness',
    label: 'Runtime',
    message: 'No live runtime is bound yet.',
    detail: 'No child runtime is currently bound to this instance.',
    tone: 'attention',
  },
];

/**
 * A benign all-defaults projection useful for fixtures that don't care
 * about cockpit verdicts.  Resume / Pause enabled as durable-only
 * writes; flatten-and-pause / stop / mark-poisoned disabled with
 * NO_LIVE_BINDING (unbound default).  Trading session is UNKNOWN so
 * tests opt in to a specific phase.
 */
export const DEFAULT_OPERATOR_SURFACE: OperatorSurface = {
  schema_version: 1,
  host_process: {
    state: 'IDLE',
    notice: null,
    copyable_command: null,
    last_exit_error_code: null,
    last_exit_error_message: null,
    last_exit_error_detail: {},
    start_capability: {
      enabled: false,
      run_id: null,
      request: null,
      disabled_reason_code: 'START_SETTINGS_INCOMPLETE',
      gate_results: [
        _gateResult(
          'host_process.start',
          'block',
          'START_SETTINGS_INCOMPLETE',
          'START_SETTINGS_INCOMPLETE',
        ),
      ],
    },
  },
  prior_run: { classification: 'UNKNOWN' },
  broker: {
    safety_verdict: 'UNKNOWN',
    connection: 'UNKNOWN',
    connection_condition: {
      code: 'BROKER_CONNECTION_UNKNOWN',
      severity: 'warning',
      title: 'Broker connection unproven',
      summary: 'The backend does not have enough runtime evidence to prove the broker connection state.',
      remediation: 'Start or refresh the live runtime so broker proof can be recorded.',
    },
  },
  configuration: { verdict: 'UNKNOWN', reason_codes: [] },
  current_risk: {
    posture: 'UNKNOWN',
    owned_positions: {},
    pending_order_count: null,
    verdict: 'UNKNOWN',
    unrealized_pnl: null,
  },
  daily_order_cap: { used: null, limit: null },
  action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
  account_owner: null,
  submit_readiness: {
    code: 'safe_to_monitor',
    label: 'Safe to monitor',
    explanation: 'The cockpit can observe this bot, but order submission is not currently active or appropriate.',
    can_submit: false,
    blocking_reason_codes: ['HOST_PROCESS_IDLE'],
    template_id: 'operator_surface.submit_readiness.safe_to_monitor',
    template_version: 1,
  },
  trader_guidance: {
    situation_code: 'monitor_only',
    headline: 'This bot is safe to monitor, not safe to submit right now.',
    explanation: 'The current state is observable, but at least one non-critical condition means order submission should not be treated as active.',
    risk_headline: 'Observation is okay; trading is not active',
    risk_explanation: 'Keep watching the bot, but do not interpret the Overview as a trade-permission signal.',
    primary_remediation: { kind: 'none', reason: 'MONITOR_ONLY' },
    additional_attention_groups: [],
    proof_lines: DEFAULT_PROOF_LINES,
    advanced_evidence: [],
    template_id: 'operator_surface.trader_guidance.monitor_only',
    template_version: 1,
  },
  blockage_ladder: {
    headline: 'Bot process is not running',
    summary: 'Host process is IDLE; live-only commands cannot execute until a bot process is started.',
    current_stage_id: 'host_process',
    stages: [
      {
        id: 'control_plane',
        label: 'Control plane',
        state: 'unknown',
        severity: 'neutral',
        current: false,
        title: 'Daemon control plane is not configured',
        summary: 'No live-runner daemon URL is configured for this data plane.',
        next_step: null,
        reason_codes: [],
      },
      {
        id: 'host_process',
        label: 'Host process',
        state: 'warning',
        severity: 'warning',
        current: true,
        title: 'Bot process is not running',
        summary: 'Host process is IDLE; live-only commands cannot execute until a bot process is started.',
        next_step: 'Use this as context for the blocked broker/reconciliation proofs, not as a separate broker problem.',
        reason_codes: ['HOST_PROCESS_IDLE'],
      },
      {
        id: 'broker',
        label: 'Broker proof',
        state: 'warning',
        severity: 'warning',
        current: false,
        title: 'Broker connection unproven',
        summary: 'Broker connection has not been proven because no live runtime is currently bound.',
        next_step: 'Start a bot process only after IBKR positions/executions are manually verified; broker proof cannot refresh while no runtime is bound.',
        reason_codes: ['BROKER_CONNECTION_UNKNOWN'],
      },
    ],
  },
  run_signal: {
    state_label: 'Off',
    tone: 'off',
    title: 'Bot process is not running',
    detail: 'Host process is IDLE; live-only commands cannot execute until a bot process is started.',
  },
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
  blockers: [],
  runtime_freshness: null,
  control_plane: null,
  broker_observation_consistency: null,
  reconciliation: {
    state: 'NOT_AVAILABLE',
    failure_reason: null,
    adopted_intent_ids: [],
    last_reconcile_ms: null,
    sidecar_wal_seq: null,
    broker_observed_at_ms: null,
  },
  // PR 2 — post-halt watchdog incident headline.
  incident_headline: null,
  // PR 5 — broker-activity publisher health surface.
  broker_activity_health: null,
  notice_placement: {
    banner: null,
    banner_folded: [],
    banner_fold_count: 0,
    attention: [],
    quiet_status: [],
  },
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

export const RESUME_DISABLED_DESIRED_STATE_ALREADY_RUNNING: ActionCapability =
  _capability(false, 'LIVE_ACTUATION', 'DESIRED_STATE_ALREADY_RUNNING', [
    'DESIRED_STATE_ALREADY_RUNNING',
  ]);

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
  gate_result: _gateResult('broker_connection', 'pass', 'connected', 'GATE_PASSING'),
  suggested_action: null,
  suggested_action_unavailable_reason: 'GATE_PASSING',
};

export const OPERATOR_GATE_FAILING_REDEPLOY: OperatorGate = {
  name: 'poison_sentinel',
  status: 'fail',
  severity: 'hard',
  detail: 'poisoned.flag present',
  gate_result: _gateResult('poison_sentinel', 'block', 'poisoned.flag present', 'redeploy'),
  suggested_action: SUGGESTED_REDEPLOY,
  suggested_action_unavailable_reason: null,
};

export const OPERATOR_GATE_FAILING_NO_INLINE_REMEDIATION: OperatorGate = {
  name: 'daily_order_cap',
  status: 'fail',
  severity: 'hard',
  detail: '50 / 50 orders used',
  gate_result: _gateResult('daily_order_cap', 'block', '50 / 50 orders used', 'NO_INLINE_REMEDIATION'),
  suggested_action: null,
  suggested_action_unavailable_reason: 'NO_INLINE_REMEDIATION',
};

export const OPERATOR_GATE_UNKNOWN_NAME: OperatorGate = {
  name: 'totally_invented_gate',
  status: 'fail',
  severity: 'hard',
  detail: '',
  gate_result: _gateResult('totally_invented_gate', 'block', '', 'UNKNOWN_GATE_NAME'),
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

// ---------------------------------------------------------------------------
// PR 1 — OperatorNotice / OperatorSurfaceRuntimeFreshness fixtures
// ---------------------------------------------------------------------------

/** A single runtime.market_data_stale notice for the bar_loop domain. */
export const STALE_BAR_LOOP_NOTICE: OperatorNotice = {
  code: 'runtime.market_data_stale',
  tier: 'warning',
  title: 'Market data is stale',
  message:
    'The most recent bar is older than the freshness window. New trading decisions are held until fresh data arrives.',
  source_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
  forensic_facts: { bar_loop_age_ms: 90_000 },
  actionability: 'self_resolving',
  resolution: 'Fresh market data arrives.',
  remedy_status: null,
  action: { kind: 'none', label: null, target: null },
  runbook_slug: 'runtime-freshness',
  occurred_at_ms: 1_700_000_090_000,
};

/** OperatorSurfaceRuntimeFreshness with a stale bar_loop and headline notice. */
export const RUNTIME_FRESHNESS_BAR_LOOP_STALE: OperatorSurfaceRuntimeFreshness = {
  posture_demoted: true,
  stale_reason_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
  command_loop: { state: 'FRESH', age_ms: 5_000, stale_reason_codes: [] },
  broker: { state: 'FRESH', age_ms: 8_000, stale_reason_codes: [] },
  bar_loop: {
    state: 'STALE',
    age_ms: 90_000,
    stale_reason_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
  },
  control_plane: { state: 'FRESH', age_ms: 3_000, stale_reason_codes: [] },
  headline: STALE_BAR_LOOP_NOTICE,
  // Backend pre-filters: the headline is excluded from additional_reasons.
  additional_reasons: [],
};
