// PRD #617 — Playwright Bot Control fixture builders.  Returns complete
// LiveInstanceStatus / LiveInstanceSummary / FleetAccountSummary
// payloads covering every shape PRD #616 added.

const _capability = (
  enabled: boolean,
  effect: 'DURABLE_ONLY' | 'LIVE_ACTUATION',
  code: string | null = null,
  reasons: string[] = [],
) => ({ enabled, effect, disabled_reason_code: code, disabled_reasons: reasons });

export interface BotControlScenarioOptions {
  strategyInstanceId: string;
  processState?: 'running' | 'idle' | 'exited' | 'stopping' | 'unreachable';
  readinessVerdict?: 'READY' | 'BLOCKED' | 'DEGRADED' | 'UNKNOWN';
  intent?: 'RUNNING' | 'PAUSED' | 'STOPPED' | null;
  brokerSafety?: 'PAPER_ONLY' | 'UNSAFE' | 'UNKNOWN';
  brokerConnection?: 'CONNECTED' | 'DISCONNECTED' | 'UNKNOWN';
  resume?: ReturnType<typeof _capability>;
  pause?: ReturnType<typeof _capability>;
  stop?: ReturnType<typeof _capability>;
  flattenAndPause?: ReturnType<typeof _capability>;
  markPoisoned?: ReturnType<typeof _capability>;
  ownedPositions?: Record<string, number>;
  poisoned?: boolean;
  readinessGates?: {
    name: string;
    status: 'pass' | 'fail' | 'unknown';
    severity: 'hard' | 'soft';
    detail: string;
    suggested_action: unknown | null;
    suggested_action_unavailable_reason: string | null;
  }[];
}

export function buildScenarioStatus(opts: BotControlScenarioOptions) {
  const processState = opts.processState ?? 'running';
  const readinessVerdict = opts.readinessVerdict ?? 'READY';
  const intent = opts.intent === undefined ? null : opts.intent;
  const brokerSafety = opts.brokerSafety ?? 'PAPER_ONLY';
  const brokerConnection = opts.brokerConnection ?? 'CONNECTED';
  const ownedPositions = opts.ownedPositions ?? {};

  return {
    strategy_instance_id: opts.strategyInstanceId,
    process: { state: processState, pid: 1, bound_run_id: 'run-1', started_at_ms: 1_700_000_000_000 },
    live_binding: processState === 'running' ? { run_id: 'run-1', run_dir: '/tmp/run-1', source: 'registry' } : null,
    evidence_binding: { run_id: 'run-1', state: 'latest_run_by_ledger', is_live: false },
    desired_state: intent ? { state: intent, path_status: 'ok', updated_at_ms: 1, updated_by: 'op', reason: null, version: 1 } : { state: null, path_status: 'absent', updated_at_ms: null, updated_by: null, reason: null, version: null },
    readiness: {
      kind: 'live_readiness',
      as_of_ms: 1_700_000_000_000,
      source: 'engine',
      verdict: readinessVerdict,
      summary: '',
      gates: (opts.readinessGates ?? []).map((g) => ({ name: g.name, status: g.status, severity: g.severity, detail: g.detail })),
      orders_used: 3,
      orders_cap: 50,
    },
    latest_decision: null,
    decision_columns: [],
    broker: {
      bot_order_namespace: 'ns',
      owned_positions: ownedPositions,
      pending_order_count: 0,
      unrealized_pnl: null,
    },
    start_defaults: {
      strategy: 'spy_ema',
      readonly: false,
      hydrate_policy: 'optional',
      max_orders_per_day: 50,
      ibkr_host: 'host',
    },
    provenance: {
      run_id: 'run-1',
      schema_version: '1.0',
      code_sha: 'abc12345def',
      strategy_spec_path: 'specs/spy_ema.yaml',
      strategy_spec_sha256: 'def456',
      qc_audit_copy_path: 'references/qc-shadow/spy.json',
      qc_audit_copy_sha256: 'ghi789',
      qc_cloud_backtest_id: 'bt-001',
      account_id: 'DU284968',
      start_date_ms: 1_700_000_000_000,
      created_at_ms: 1_700_000_000_000,
      live_config: { symbol: 'SPY' },
    },
    sizing: null,
    last_exit: opts.poisoned ? { run_id: 'run-1', halt_trigger: 'OPERATOR_DECLARED', halt_at_ms: 1, halt_detail: null, ended_at_ms: 1, exit_code: 1, exit_reason: 'fatal_halt', hydration_accepted: null, hydration_failure_reason: null } : null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: {
      schema_version: 1,
      host_process: {
        state: processState === 'running' ? 'RUNNING' : processState === 'idle' ? (intent === 'RUNNING' ? 'WAITING_FOR_HOST' : 'IDLE') : processState.toUpperCase(),
        notice: processState === 'running' ? null : 'Host process is not running.',
        copyable_command: null,
        // ADR 0013 amendment 2026-06-22 — start_capability is a required
        // field on OperatorSurfaceHostProcess. e2e default mirrors the
        // "no live binding" disabled case; per-scenario overrides may
        // flip this when testing the Start affordance.
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: opts.poisoned
            ? 'STOPPED_REQUIRES_REDEPLOY'
            : processState === 'running'
              ? 'ALREADY_RUNNING'
              : 'START_SETTINGS_INCOMPLETE',
        },
      },
      prior_run: { classification: opts.poisoned ? 'HALT_TRIGGERED' : 'UNKNOWN' },
      broker: { safety_verdict: brokerSafety, connection: brokerConnection },
      configuration: { verdict: 'READY', reason_codes: [] },
      current_risk: {
        posture: Object.keys(ownedPositions).length ? 'LONG' : 'FLAT',
        pending_order_count: 0,
        verdict: 'READY',
        unrealized_pnl: null,
      },
      daily_order_cap: { used: 3, limit: 50 },
      action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
      actions: {
        resume: opts.resume ?? _capability(intent !== 'RUNNING' && !opts.poisoned, processState === 'running' ? 'LIVE_ACTUATION' : 'DURABLE_ONLY'),
        pause: opts.pause ?? _capability(intent !== 'PAUSED' && !opts.poisoned, processState === 'running' ? 'LIVE_ACTUATION' : 'DURABLE_ONLY'),
        stop: opts.stop ?? _capability(intent !== 'STOPPED' && !opts.poisoned, processState === 'running' ? 'LIVE_ACTUATION' : 'DURABLE_ONLY'),
        flatten_and_pause: opts.flattenAndPause ?? _capability(
          processState === 'running' && Object.keys(ownedPositions).length > 0,
          'LIVE_ACTUATION',
          Object.keys(ownedPositions).length === 0 ? 'NO_OWNED_POSITIONS' : null,
          Object.keys(ownedPositions).length === 0 ? ['NO_OWNED_POSITIONS'] : [],
        ),
        mark_poisoned: opts.markPoisoned ?? _capability(processState === 'running' && !opts.poisoned, 'LIVE_ACTUATION'),
      },
      trading_session: {
        phase: 'RTH',
        permits_strategy_activity: true,
        next_transition_ms: 1_700_000_000_000 + 3_600_000,
        timezone: 'America/New_York',
        as_of_ms: 1_700_000_000_000,
      },
      readiness_gates: (opts.readinessGates ?? []).map((g) => ({
        name: g.name,
        status: g.status,
        severity: g.severity,
        detail: g.detail,
        suggested_action: g.suggested_action,
        suggested_action_unavailable_reason: g.suggested_action_unavailable_reason,
      })),
    },
    fetched_at_ms: 1_700_000_000_000,
  };
}

export function buildSummary(opts: {
  strategyInstanceId: string;
  processState?: string;
  readinessVerdict?: 'READY' | 'BLOCKED' | 'DEGRADED' | 'UNKNOWN';
  desiredState?: string | null;
  boundRunId?: string | null;
}) {
  return {
    strategy_instance_id: opts.strategyInstanceId,
    process_state: opts.processState ?? 'running',
    bound_run_id: opts.boundRunId ?? 'run-1',
    latest_run_id: opts.boundRunId ?? 'run-1',
    desired_state: opts.desiredState ?? null,
    readiness_verdict: opts.readinessVerdict ?? 'READY',
    readiness_as_of_ms: 1_700_000_000_000,
  };
}

export function buildAccountSummary(opts: {
  identity?: 'CONSISTENT' | 'CONFLICTING' | 'UNKNOWN';
  contamination?: 'clean' | 'contaminated' | 'unknown';
  policyBlocksStarts?: boolean;
  accountId?: string | null;
}) {
  const identity = opts.identity ?? 'CONSISTENT';
  const contamination = opts.contamination ?? 'clean';
  return {
    account_id: opts.accountId === undefined ? 'DU284968' : opts.accountId,
    account_identity: identity,
    account_identity_reason_codes:
      identity === 'CONFLICTING'
        ? ['INSTANCE_ACCOUNT_MISMATCH']
        : identity === 'UNKNOWN'
        ? ['BROKER_ACCOUNT_UNAVAILABLE']
        : [],
    contamination: {
      net_positions: contamination === 'unknown' ? null : {},
      explained_total: {},
      explained_by_instance: [],
      residual: contamination === 'contaminated' ? { SPY: 1 } : {},
      verdict: contamination,
      policy_blocks_starts: !!opts.policyBlocksStarts,
      summary:
        contamination === 'clean'
          ? 'Account clean — every position is explained by a managed instance.'
          : contamination === 'contaminated'
          ? 'Account residual: SPY +1 unattributed outside managed namespaces.'
          : 'Net account position unavailable — contamination unknown.',
    },
  };
}
