import type {
  ActionCapability,
  FleetAccountSummary,
  GateSuggestedAction,
  HostProcessState,
  LifecycleChartAction,
  LifecycleTimelineResponse,
  LiveInstanceStatus,
  LiveInstanceSummary,
  OperatorGate,
  OperatorNotice,
  ReadinessVerdict,
} from '../../../src/app/api/live-instances.types';
import type { LiveInstanceActivityProjection } from '../../../src/app/components/broker/bot-control/reused/bot-trade-chart-card/bot-trade-chart-card.types';
import { makeLifecycleChartFixture } from '../../../src/app/testing/live-instance-status-fixtures';
import { makeOperatorSurfaceFixture } from '../../../src/app/testing/operator-surface-fixtures';

const NOW_MS = 1_800_000_000_000;

const capability = (
  enabled: boolean,
  effect: ActionCapability['effect'],
  code: string | null = null,
  reasons: string[] = [],
): ActionCapability => ({
  enabled,
  effect,
  disabled_reason_code: code,
  disabled_reasons: reasons,
  gate_results: [],
});

interface ScenarioReadinessGate {
  name: string;
  status: 'pass' | 'fail' | 'unknown';
  severity: 'hard' | 'soft';
  detail: string;
  suggested_action: GateSuggestedAction | null;
  suggested_action_unavailable_reason: string | null;
}

export interface BotControlScenarioOptions {
  strategyInstanceId: string;
  processState?: 'running' | 'idle' | 'exited' | 'stopping' | 'unreachable';
  readinessVerdict?: ReadinessVerdict;
  intent?: 'RUNNING' | 'PAUSED' | 'STOPPED' | null;
  brokerSafety?: 'PAPER_ONLY' | 'UNSAFE' | 'UNKNOWN';
  brokerConnection?: 'CONNECTED' | 'DISCONNECTED' | 'UNKNOWN';
  controlPlaneState?: 'CONNECTED' | 'RETRYING' | 'UNREACHABLE' | null;
  resume?: ActionCapability;
  pause?: ActionCapability;
  stop?: ActionCapability;
  flattenAndPause?: ActionCapability;
  markPoisoned?: ActionCapability;
  ownedPositions?: Record<string, number>;
  poisoned?: boolean;
  readinessGates?: ScenarioReadinessGate[];
}

export function buildScenarioStatus(opts: BotControlScenarioOptions): LiveInstanceStatus {
  const processState = opts.processState ?? 'running';
  const readinessVerdict = opts.readinessVerdict ?? 'READY';
  const intent = opts.intent === undefined ? 'RUNNING' : opts.intent;
  const brokerSafety = opts.brokerSafety ?? 'PAPER_ONLY';
  const brokerConnection = opts.brokerConnection ?? 'CONNECTED';
  const ownedPositions = opts.ownedPositions ?? {};
  const id = opts.strategyInstanceId;
  const baseSurface = makeOperatorSurfaceFixture();
  const actions = {
    resume: opts.resume ?? capability(intent !== 'RUNNING' && !opts.poisoned, processState === 'running' ? 'LIVE_ACTUATION' : 'DURABLE_ONLY'),
    pause: opts.pause ?? capability(intent !== 'PAUSED' && !opts.poisoned, processState === 'running' ? 'LIVE_ACTUATION' : 'DURABLE_ONLY'),
    stop: opts.stop ?? capability(intent !== 'STOPPED' && !opts.poisoned, processState === 'running' ? 'LIVE_ACTUATION' : 'DURABLE_ONLY'),
    flatten_and_pause: opts.flattenAndPause ?? capability(
      processState === 'running' && Object.keys(ownedPositions).length > 0,
      'LIVE_ACTUATION',
      Object.keys(ownedPositions).length === 0 ? 'NO_OWNED_POSITIONS' : null,
      Object.keys(ownedPositions).length === 0 ? ['NO_OWNED_POSITIONS'] : [],
    ),
    mark_poisoned: opts.markPoisoned ?? capability(processState === 'running' && !opts.poisoned, 'LIVE_ACTUATION'),
  };
  const hostState = hostProcessState(processState, intent);
  const readinessGates = opts.readinessGates ?? [];
  const attentionGroups = [
    ...(readinessVerdict === 'READY'
      ? []
      : [{
          code: 'readiness_blocked',
          severity: 'warning' as const,
          headline: 'Readiness needs attention',
          explanation: 'The backend readiness vector is not ready for live submission.',
          operator_next_step: 'Inspect the readiness gates before attempting submit.',
          remediation: { kind: 'none' as const, reason: 'MONITOR_ONLY' },
        }]),
    ...(brokerConnection === 'CONNECTED'
      ? []
      : [{
          code: 'broker_connection',
          severity: 'warning' as const,
          headline: processState === 'running' ? 'Broker disconnected or unknown' : 'Broker proof waits for a live runtime',
          explanation: processState === 'running'
            ? 'The backend cannot prove an active broker session for this bot.'
            : 'Broker connection has not been proven because no live runtime is currently bound.',
          operator_next_step: processState === 'running'
            ? 'Reconnect the broker session, then refresh broker evidence.'
            : 'Start a bot process only after IBKR positions/executions are manually verified; broker proof cannot refresh while no runtime is bound.',
          remediation: processState === 'running'
            ? { kind: 'open_runbook' as const, slug: 'broker-reconnect' }
            : { kind: 'none' as const, reason: 'WAITING_FOR_LIVE_RUNTIME' },
        }]),
  ];
  const chart = makeLifecycleChartFixture({
    selected_bot_id: id,
    actions: [
      lifecycleAction('resume', 'Resume', actions.resume, 'activate', 'primary'),
      lifecycleAction('pause', 'Pause', actions.pause, 'active', 'secondary'),
      lifecycleAction('flatten_and_pause', 'Flatten and pause', actions.flatten_and_pause, 'recovery', 'danger'),
      lifecycleAction('redeploy', 'Fresh run', capability(true, 'DURABLE_ONLY'), 'deploy', 'secondary'),
    ],
  });
  return {
    strategy_instance_id: id,
    process: {
      state: processState,
      pid: processState === 'running' ? 1 : null,
      bound_run_id: processState === 'running' ? 'run-1' : null,
      started_at_ms: processState === 'running' ? NOW_MS - 60_000 : null,
    },
    live_binding: processState === 'running'
      ? { run_id: 'run-1', run_dir: '/tmp/run-1', source: 'registry' }
      : null,
    evidence_binding: { run_id: 'run-1', state: 'latest_run_by_ledger', is_live: processState === 'running' },
    desired_state: intent
      ? { state: intent, path_status: 'ok', updated_at_ms: NOW_MS - 1_000, updated_by: 'op', reason: null, version: 1 }
      : { state: null, path_status: 'absent', updated_at_ms: null, updated_by: null, reason: null, version: null },
    readiness: {
      kind: 'live_readiness',
      as_of_ms: NOW_MS,
      source: 'engine',
      verdict: readinessVerdict,
      summary: readinessVerdict === 'READY' ? 'Ready to submit.' : 'Readiness is blocked.',
      gates: readinessGates.map((g) => ({
        name: g.name,
        status: g.status,
        severity: g.severity,
        detail: g.detail,
      })),
    },
    latest_decision: null,
    decision_columns: [],
    broker: {
      bot_order_namespace: 'ns',
      owned_positions: ownedPositions,
      pending_order_count: 0,
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
      start_date_ms: NOW_MS - 86_400_000,
      created_at_ms: NOW_MS - 60_000,
      live_config: { symbol: 'SPY' },
    },
    sizing: null,
    last_exit: opts.poisoned
      ? { run_id: 'run-1', halt_trigger: 'OPERATOR_DECLARED', halt_at_ms: NOW_MS, halt_detail: null, ended_at_ms: NOW_MS, exit_code: 1, exit_reason: 'fatal_halt', hydration_accepted: null, hydration_failure_reason: null }
      : null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: makeOperatorSurfaceFixture({
      host_process: {
        ...baseSurface.host_process,
        state: hostState,
        notice: hostState === 'RUNNING' ? null : 'Host process is not running.',
        start_capability: {
          ...baseSurface.host_process.start_capability,
          enabled: false,
          disabled_reason_code: hostState === 'RUNNING' ? 'ALREADY_RUNNING' : 'START_SETTINGS_INCOMPLETE',
        },
      },
      broker: { safety_verdict: brokerSafety, connection: brokerConnection },
      configuration: { verdict: readinessVerdict === 'READY' ? 'READY' : 'ATTENTION', reason_codes: [] },
      current_risk: {
        posture: Object.keys(ownedPositions).length ? 'LONG' : 'FLAT',
        pending_order_count: 0,
        verdict: readinessVerdict === 'READY' ? 'READY' : 'ATTENTION',
        unrealized_pnl: null,
      },
      daily_order_cap: { used: 3, limit: 50 },
      action_plan: { consumption: 'ACTIVE', anomaly_verdict: 'READY' },
      submit_readiness: {
        ...baseSurface.submit_readiness,
        code: readinessVerdict === 'READY' ? 'safe_to_submit' : 'blocked_before_submit',
        label: readinessVerdict === 'READY' ? 'Safe to submit' : 'Blocked before submit',
        explanation: readinessVerdict === 'READY'
          ? 'Broker safety and submit-readiness proofs are satisfied.'
          : 'The backend says this bot must not submit yet.',
        can_submit: readinessVerdict === 'READY',
        blocking_reason_codes: readinessVerdict === 'READY' ? [] : ['READINESS_BLOCKED'],
      },
      trader_guidance: {
        ...baseSurface.trader_guidance,
        situation_code: readinessVerdict === 'READY' ? 'ready_to_submit' : 'submission_blocked',
        headline: readinessVerdict === 'READY'
          ? 'This bot is ready to submit paper orders.'
          : 'This bot needs attention before it can submit.',
        explanation: readinessVerdict === 'READY'
          ? 'All backend submit-readiness proofs are currently satisfied.'
          : 'The backend readiness vector or broker proof is blocking submission.',
        risk_headline: readinessVerdict === 'READY' ? 'Submission gates are satisfied' : 'Do not submit while blocked',
        risk_explanation: readinessVerdict === 'READY'
          ? 'The broker, submit lane, owner generation, and reconciliation proofs are present.'
          : 'Reconnect or reconcile until the broker evidence is fresh and explicit.',
        primary_remediation: readinessVerdict === 'READY'
          ? { kind: 'none', reason: 'READY' }
          : {
              kind: 'invoke_endpoint',
              endpoint: 'reconcile_instance',
              method: 'POST',
              path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
            },
        additional_attention_groups: attentionGroups,
      },
      actions,
      trading_session: {
        phase: 'RTH',
        permits_strategy_activity: true,
        next_transition_ms: NOW_MS + 3_600_000,
        timezone: 'America/New_York',
        as_of_ms: NOW_MS,
      },
      readiness_gates: readinessGates.map(toOperatorGate),
      control_plane: opts.controlPlaneState === undefined || opts.controlPlaneState === null
        ? null
        : {
            state: opts.controlPlaneState,
            last_transition_ms: NOW_MS - 30_000,
            last_success_ms: opts.controlPlaneState === 'CONNECTED' ? NOW_MS - 30_000 : null,
            attempt: opts.controlPlaneState === 'RETRYING' ? 2 : 0,
            daemon_boot_id: 'daemon-1',
            notice: opts.controlPlaneState === 'CONNECTED' ? null : 'The control plane is not connected.',
            runbook_slug: 'control-plane',
          },
    }),
    lifecycle_chart: chart,
    fetched_at_ms: NOW_MS,
  };
}

export function buildSummary(opts: {
  strategyInstanceId: string;
  processState?: LiveInstanceSummary['process_state'];
  readinessVerdict?: ReadinessVerdict;
  desiredState?: string | null;
  boundRunId?: string | null;
}): LiveInstanceSummary {
  return {
    strategy_instance_id: opts.strategyInstanceId,
    process_state: opts.processState ?? 'running',
    bound_run_id: opts.boundRunId ?? 'run-1',
    latest_run_id: opts.boundRunId ?? 'run-1',
    desired_state: opts.desiredState ?? 'RUNNING',
    readiness_verdict: opts.readinessVerdict ?? 'READY',
    readiness_as_of_ms: NOW_MS,
  };
}

export function buildAccountSummary(opts: {
  identity?: 'CONSISTENT' | 'CONFLICTING' | 'UNKNOWN';
  contamination?: 'clean' | 'contaminated' | 'unknown';
  policyBlocksStarts?: boolean;
  accountId?: string | null;
  notice?: OperatorNotice | null;
} = {}): FleetAccountSummary {
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
          ? 'Account clean: every position is explained by a managed instance.'
          : contamination === 'contaminated'
            ? 'Account residual: SPY +1 unattributed outside managed namespaces.'
            : 'Net account position unavailable: contamination unknown.',
    },
    notice: opts.notice ?? null,
  };
}

export function buildLifecycleTimeline(sid: string): LifecycleTimelineResponse {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    rows: [
      {
        id: 101,
        account_id: 'DU284968',
        strategy_instance_id: sid,
        run_id: 'run-1',
        event_id: 'intent_wal:run-1:7:ACK_FAILED_UNCERTAIN',
        event_type: 'BrokerOrderUncertain',
        category: 'order',
        node_id: 'ack_or_reconcile',
        gate_id: null,
        status: 'blocked',
        severity: 'warning',
        ts_ms: NOW_MS - 10_000,
        ts_ms_resolved: true,
        source_artifact: 'intent_events.jsonl',
        source_type: 'broker_ack',
        source_rank: 30,
        source_seq: 7,
        source_offset: null,
        source_hash: null,
        summary: 'Broker acknowledgment failed; submit outcome is uncertain.',
        why: 'Probe broker before retrying this intent.',
        operator_next_step: 'PROBE_BROKER_BEFORE_RETRY',
        receipt_payload: { intent_id: 'intent-7', order_ref: 'learn-ai/sid/v1:intent-7' },
        evidence_refs: [],
        rendered_headline: null,
        rendered_template_id: null,
        inserted_at_ms: NOW_MS - 9_000,
        updated_at_ms: NOW_MS - 9_000,
      },
    ],
  };
}

export function buildActivityProjection(sid: string): LiveInstanceActivityProjection {
  return {
    schema_version: 1,
    strategy_instance_id: sid,
    session_date: '2026-07-02',
    timezone: 'America/New_York',
    symbol: 'SPY',
    resolution: '1m',
    has_bars: false,
    now_ms: NOW_MS,
    bars: [],
    fill_markers: [],
    position_annotations: [],
    order_overlays: [],
    orders_today: [],
    broker_activity_rows: [],
    position_snapshot: [],
    reconciliation_warnings: [],
    evidence: [],
  };
}

export function buildChartSnapshot(sid: string): Record<string, unknown> {
  return {
    date: '2026-07-02',
    symbol: 'SPY',
    resolution: '1m',
    has_bars: false,
    now_ms: NOW_MS,
    bars: [],
    runs: [
      {
        run_id: `${sid}:run-1`,
        started_at_ms: NOW_MS - 60_000,
        ended_at_ms: null,
        is_current: true,
        color_index: 0,
        trades: [],
        executions: [],
      },
    ],
  };
}

function hostProcessState(
  processState: BotControlScenarioOptions['processState'],
  intent: BotControlScenarioOptions['intent'],
): HostProcessState {
  if (processState === 'running') return 'RUNNING';
  if (processState === 'stopping') return 'STOPPING';
  if (processState === 'unreachable') return 'UNREACHABLE';
  if (processState === 'idle' && intent === 'RUNNING') return 'WAITING_FOR_HOST';
  if (processState === 'exited') return 'EXITED';
  return 'IDLE';
}

function lifecycleAction(
  id: LifecycleChartAction['id'],
  label: string,
  action: ActionCapability,
  targetNodeId: string,
  tone: LifecycleChartAction['tone'],
): LifecycleChartAction {
  return {
    id,
    label,
    enabled: action.enabled,
    reason_code: action.disabled_reason_code,
    reason_headline: action.enabled ? 'Available' : 'Unavailable',
    reason_detail: action.enabled ? `${label} is available for this bot.` : `${label} is currently disabled by backend policy.`,
    target_node_id: targetNodeId,
    tone,
  };
}

function toOperatorGate(gate: ScenarioReadinessGate): OperatorGate {
  const status = gate.status === 'pass' ? 'pass' : gate.status === 'fail' ? 'block' : 'unknown';
  return {
    name: gate.name,
    status,
    severity: gate.severity,
    detail: gate.detail,
    gate_result: {
      gate_id: gate.name,
      status,
      source: 'e2e_fixture',
      operator_reason: gate.detail,
      operator_next_step: null,
      evidence_at_ms: NOW_MS,
    },
    suggested_action: gate.suggested_action,
    suggested_action_unavailable_reason: gate.suggested_action_unavailable_reason,
  };
}
