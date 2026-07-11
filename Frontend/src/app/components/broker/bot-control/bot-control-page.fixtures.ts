import type {
  BotLifecycleMutationResponse,
  FleetAccountSummary,
  HostProcessState,
  LifecycleTimelineResponse,
  LiveInstanceStatus,
  MutationRungReceipt,
  OperatorNotice,
  OperatorSurfaceRuntimeFreshness,
  SetInstanceDesiredStateResponse,
} from '../../../api/live-instances.types';
import type {
  CommandVerb,
  CommandWriteResponse,
  HostRunnerHealth,
  HostRunnerStartRequest,
  ReconcileAckResponse,
} from '../../../api/live-runs.types';
import {
  makeDailyLifecycleFixture,
  makeLifecycleChartFixture,
} from '../../../testing/live-instance-status-fixtures';

export function makeStatus(options: {
  id?: string;
  hostState?: HostProcessState;
  hostNotice?: string;
  runSignal?: LiveInstanceStatus['operator_surface']['run_signal'];
  startCapabilityEnabled?: boolean;
  startRunId?: string;
  startRequest?: HostRunnerStartRequest;
  markPoisonedEnabled?: boolean;
} = {}): LiveInstanceStatus {
  const hostState = options.hostState ?? 'UNREACHABLE';
  const hostNotice = options.hostNotice ?? 'Start the host runner before trading this bot.';
  const startRequest: HostRunnerStartRequest = options.startRequest ?? {
    readonly: false,
    hydrate_policy: 'require',
    strategy: 'deployment_validation',
    max_orders_per_day: 2,
    ibkr_host: '127.0.0.1',
  };
  const processState: LiveInstanceStatus['process']['state'] = hostState === 'WAITING_FOR_HOST'
    ? 'idle'
    : hostState === 'RUNNING'
      ? 'running'
      : 'exited';
  return {
    stream_epoch: 'fixture-epoch',
    surface_version: 1,
    strategy_instance_id: options.id ?? 'sid-x',
    process: { state: processState, pid: null, bound_run_id: null, started_at_ms: null },
    live_binding: null,
    evidence_binding: null,
    latest_mutation: null,
    desired_state: {
      state: 'RUNNING',
      path_status: 'ok',
      updated_at_ms: 0,
      updated_by: 'op',
      reason: null,
      version: 1,
    },
    readiness: null,
    latest_decision: null,
    latest_signal_tone: 'neutral',
    decision_columns: [],
    broker: null,
    start_defaults: null,
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: {
      schema_version: 1,
      host_process: {
        state: hostState,
        notice: hostNotice,
        copyable_command: hostState === 'UNREACHABLE' ? 'make broker-runner' : null,
        last_exit_error_code: null,
        last_exit_error_message: null,
        last_exit_error_detail: {},
        start_capability: options.startCapabilityEnabled
          ? {
              enabled: true,
              run_id: options.startRunId ?? 'run-x',
              request: startRequest,
              disabled_reason_code: null,
              gate_results: [],
            }
          : {
              enabled: false,
              run_id: null,
              request: null,
              disabled_reason_code: 'HOST_SERVICE_OFFLINE',
              gate_results: [],
            },
      },
      prior_run: { classification: 'UNKNOWN' },
      broker: {
        safety_verdict: 'UNKNOWN',
        connection: 'DISCONNECTED',
        connection_condition: {
          code: 'BROKER_DISCONNECTED',
          severity: 'warning',
          title: 'Broker session disconnected',
          summary: 'The runtime cannot prove an active IBKR broker session.',
          remediation: 'Reconnect the broker session, then refresh broker evidence.',
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
        code: 'broker_state_unproven',
        label: 'Broker state unproven',
        explanation: 'The backend cannot prove the broker/session/reconciliation evidence required for a safe submit.',
        can_submit: false,
        blocking_reason_codes: ['BROKER_CONNECTION_DISCONNECTED'],
        template_id: 'operator_surface.submit_readiness.broker_state_unproven',
        template_version: 1,
      },
      trader_guidance: {
        situation_code: 'broker_state_unproven',
        headline: 'Broker state is not proven enough to submit.',
        explanation: 'The backend cannot prove the broker/session/reconciliation facts needed before a submit.',
        risk_headline: 'Do not treat stale or missing broker evidence as live truth',
        risk_explanation: 'Reconnect or reconcile until the broker evidence is fresh and explicit.',
        primary_remediation: {
          kind: 'invoke_endpoint',
          endpoint: 'reconcile_instance',
          method: 'POST',
          path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
        },
        additional_attention_groups: [
          {
            code: 'broker_connection',
            severity: 'warning',
            headline: 'Broker session is disconnected',
            explanation: 'The broker connection evidence is not connected.',
            operator_next_step: 'Reconnect the broker session, then refresh broker evidence.',
            remediation: { kind: 'open_runbook', slug: 'broker-reconnect' },
          },
        ],
        proof_lines: [
          {
            id: 'broker-proof',
            label: 'Broker',
            message: 'Broker session is disconnected.',
            detail: 'Account safety proof is not recorded. Broker session is disconnected.',
            tone: 'attention',
          },
          {
            id: 'submit-readiness',
            label: 'Trade submit',
            message: 'Broker state unproven',
            detail:
              'The backend cannot prove the broker/session/reconciliation evidence required for a safe submit. 1 blocking proof still needs attention.',
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
        ],
        advanced_evidence: [
          {
            label: 'broker.connection',
            value: 'DISCONNECTED',
            source: 'operator_surface',
            gate_id: null,
            ts_ms: null,
            ts_ms_resolved: false,
          },
        ],
        template_id: 'operator_surface.trader_guidance.broker_state_unproven',
        template_version: 1,
      },
      blockage_ladder: {
        headline: 'Broker session disconnected',
        summary: 'The broker connection evidence is not connected.',
        current_stage_id: 'broker',
        stages: [
          {
            id: 'control_plane',
            label: 'Control plane',
            state: 'warning',
            severity: 'warning',
            current: false,
            title: 'Daemon control plane needs attention',
            summary: 'Host daemon is unreachable.',
            next_step: null,
            reason_codes: ['DAEMON_UNREACHABLE'],
          },
          {
            id: 'broker',
            label: 'Broker proof',
            state: 'warning',
            severity: 'warning',
            current: true,
            title: 'Broker session disconnected',
            summary: 'The broker connection evidence is not connected.',
            next_step: 'Reconnect the broker session, then refresh broker evidence.',
            reason_codes: ['BROKER_DISCONNECTED'],
          },
        ],
      },
      run_signal: options.runSignal ?? {
        state_label: 'Needs proof',
        tone: 'attention',
        title: 'Host process',
        detail: hostNotice,
      },
      actions: {
        resume: {
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'NO_LIVE_BINDING',
          disabled_reasons: ['NO_LIVE_BINDING'],
          gate_results: [],
        },
        pause: {
          enabled: true,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [],
        },
        stop: {
          enabled: true,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [],
        },
        flatten_and_pause: {
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'NO_OWNED_POSITIONS',
          disabled_reasons: ['NO_OWNED_POSITIONS'],
          gate_results: [],
        },
        mark_poisoned: {
          enabled: options.markPoisonedEnabled ?? false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: options.markPoisonedEnabled ? null : 'NO_LIVE_BINDING',
          disabled_reasons: options.markPoisonedEnabled ? [] : ['NO_LIVE_BINDING'],
          gate_results: [],
        },
      },
      confirmations: {
        mark_poisoned: {
          title: 'Mark this run POISONED',
          body: 'Backend-authored poisoned body.',
          consequence: 'Backend-authored poisoned consequence.',
          confirm_label: 'Mark POISONED',
          required_token: 'HALT',
        },
        crash_recovery_override: {
          title: 'Confirm the broker account is flat',
          body: 'Backend-authored crash recovery body.',
          consequence: 'Backend-authored crash recovery consequence.',
          confirm_label: 'Record recovery override',
          required_token: '',
        },
        retire_replace: {
          title: 'Retire & Replace',
          body: 'Backend-authored retire body.',
          consequence: 'Backend-authored retire consequence.',
          confirm_label: 'Retire & Replace',
          required_token: '',
        },
        remove_bot: {
          title: 'Remove bot',
          body: 'Backend-authored remove body.',
          consequence: 'Backend-authored remove consequence.',
          confirm_label: 'Remove bot',
          required_token: '',
        },
      },
      trading_session: {
        phase: 'UNKNOWN',
        permits_strategy_activity: false,
        next_transition_ms: null,
        timezone: 'America/New_York',
        as_of_ms: 0,
      },
      readiness_gates: [],
      blockers: [],
      runtime_freshness: null,
      control_plane: {
        state: 'UNREACHABLE',
        last_transition_ms: 0,
        last_success_ms: null,
        attempt: 0,
        daemon_boot_id: null,
        notice: 'Last command channel health check failed.',
        runbook_slug: 'control plane/runbook?',
      },
      broker_observation_consistency: null,
      reconciliation: null,
      broker_activity_health: null,
      incident_headline: null,
      notice_placement: {
        banner: null,
        banner_fold_count: 0,
        banner_folded: [],
        attention: [],
        quiet_status: [],
      },
    },
    lifecycle_chart: makeLifecycleChartFixture(),
    daily_lifecycle: makeDailyLifecycleFixture(),
    fetched_at_ms: 0,
  };
}

export function makeAccountSummary(): FleetAccountSummary {
  return {
    account_id: 'DU1',
    account_identity: 'UNKNOWN',
    account_identity_reason_codes: [],
    contamination: {
      net_positions: null,
      explained_total: {},
      explained_by_instance: [],
      residual: {},
      verdict: 'unknown',
      policy_blocks_starts: false,
      summary: 'Broker evidence unavailable.',
    },
  };
}

export function makeIncidentHeadline(): OperatorNotice {
  return {
    code: 'watchdog.flatten_timed_out',
    tier: 'critical',
    title: 'Flatten timed out',
    message: 'The watchdog could not prove that the account is flat after the emergency flatten attempt.',
    source_codes: ['watchdog.flatten_timed_out'],
    forensic_facts: {
      run_id: 'run-x',
      attempt: 1,
    },
    actionability: 'routed',
    resolution: 'Clears after the operator verifies IBKR positions and runs Reconcile.',
    remedy_status: null,
    action: { kind: 'open_runbook', label: 'How to recover', target: 'watchdog-halt' },
    runbook_slug: 'watchdog-halt',
    occurred_at_ms: 1_700_000_001_000,
  };
}

export function makeRuntimeFreshnessWithLeaseAction(): OperatorSurfaceRuntimeFreshness {
  const headline: OperatorNotice = {
    code: 'runtime.control_plane_lease_stale',
    tier: 'critical',
    title: 'Control-plane lease is stale',
    message: 'The engine has not observed a fresh daemon lease.',
    source_codes: ['CONTROL_PLANE_LEASE_STALE'],
    forensic_facts: {},
    actionability: 'actuatable',
    resolution: 'Clears when the cockpit renews the control-plane lease and the engine reports the same lease holder.',
    remedy_status: null,
    action: {
      kind: 'renew_control_plane_lease',
      label: 'Renew control-plane lease',
      target: 'daemon_lease',
    },
    runbook_slug: 'runtime-freshness',
    occurred_at_ms: 1_700_000_001_000,
  };
  return {
    posture_demoted: true,
    stale_reason_codes: ['CONTROL_PLANE_LEASE_STALE'],
    command_loop: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
    broker: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
    bar_loop: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
    control_plane: {
      state: 'STALE',
      age_ms: 30_000,
      stale_reason_codes: ['CONTROL_PLANE_LEASE_STALE'],
    },
    headline,
    additional_reasons: [],
  };
}

export function makeLifecycleTimeline(): LifecycleTimelineResponse {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    rows: [
      {
        id: 101,
        account_id: 'DU1',
        strategy_instance_id: 'sid-x',
        run_id: 'run-x',
        event_id: 'intent_wal:run-x:7:ACK_FAILED_UNCERTAIN',
        event_type: 'BrokerOrderUncertain',
        category: 'order',
        node_id: 'ack_or_reconcile',
        gate_id: null,
        status: 'blocked',
        severity: 'warning',
        ts_ms: 1_700_000_001_000,
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
        receipt_payload: { intent_id: 'intent-7', order_ref: 'learn-ai/sid-x/v1:intent-7' },
        evidence_refs: [],
        rendered_headline: null,
        rendered_template_id: null,
        inserted_at_ms: 1_700_000_001_100,
        updated_at_ms: 1_700_000_001_100,
      },
    ],
  };
}

export function makeDesiredStateResponse(): SetInstanceDesiredStateResponse {
  return {
    mutation_attempt_id: 'mutation-fixture-1',
    mutation_dispatch_state: 'RESPONSE_CONFIRMED',
    durable: {
      state: 'PAUSED',
      updated_at_ms: 1_700_000_001_000,
      updated_by: 'operator',
      reason: 'test action accepted',
      version: 1,
    },
    actuation: {
      actuated: true,
      run_id: 'run-x',
      command_seq: 1,
      detail: 'Command accepted.',
    },
    rung_receipt: makeMutationRungReceipt(),
    rung_receipt_warnings: [],
  };
}

export function makeBotLifecycleMutationResponse(
  overrides: Partial<BotLifecycleMutationResponse> = {},
): BotLifecycleMutationResponse {
  return {
    strategy_instance_id: 'sid-x',
    lifecycle: makeDailyLifecycleFixture(),
    ...overrides,
  };
}

export function makeCommandWriteResponse(verb: CommandVerb = 'MARK_POISONED'): CommandWriteResponse {
  return {
    accepted: true,
    command: {
      seq: 1,
      verb,
      status: 'queued',
      reason: null,
      issued_by: 'operator',
      queued_at_ms: 1_700_000_001_000,
      acked_at_ms: null,
      outcome: null,
      outcome_detail: null,
    },
    rung_receipt: makeMutationRungReceipt({ title: `${verb} accepted. Next rung: Broker proof.` }),
    rung_receipt_warnings: [],
  };
}

export function makeHostRunnerProcess(): HostRunnerHealth['process'] {
  return {
    state: 'running',
    run_id: 'run-x',
    pid: 42,
    started_at_ms: 1_700_000_001_000,
    ended_at_ms: null,
    exit_code: null,
    command: [],
    log_path: '/tmp/run-x.log',
    message: 'running',
  };
}

export function makeHostRunnerHealth(): HostRunnerHealth {
  return {
    ok: true,
    repo_root: '/repo',
    live_runs_root: '/runs',
    fetched_at_ms: 1_700_000_001_000,
    process: makeHostRunnerProcess(),
  };
}

export function makeReconcileAckResponse(): ReconcileAckResponse {
  return {
    request_id: 'reconcile-request-x',
    accepted_at_ms: 1_700_000_001_000,
    rung_receipt: makeMutationRungReceipt({ title: 'Reconcile accepted. Next rung: reconciliation.' }),
    rung_receipt_warnings: [],
  };
}

export function makeMutationRungReceipt(
  overrides: Partial<MutationRungReceipt> = {},
): MutationRungReceipt {
  return {
    code: 'mutation.next_blocking_rung',
    tier: 'warning',
    title: 'Mutation accepted. Next rung: Broker proof.',
    message: 'Broker evidence is not connected.',
    rung_id: 'broker',
    source_codes: ['BROKER_DISCONNECTED'],
    forensic_facts: {},
    actionability: 'routed',
    resolution: 'Clears when broker evidence is connected again.',
    remedy_status: null,
    action: {
      kind: 'external_manual_check',
      label: 'Check IBKR session',
      target: 'ibkr_connection',
    },
    occurred_at_ms: 1_700_000_001_000,
    ...overrides,
  };
}

export function allowFlattenAndPause(status: LiveInstanceStatus): void {
  status.operator_surface.actions.flatten_and_pause = {
    enabled: true,
    effect: 'LIVE_ACTUATION',
    disabled_reason_code: null,
    disabled_reasons: [],
    gate_results: [],
  };
}
