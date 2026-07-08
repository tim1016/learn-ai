import { describe, expect, it } from 'vitest';

import type { LiveInstanceStatus, OperatorSurfaceControlPlane } from '../../../../api/live-instances.types';
import { makeLifecycleChartFixture } from '../../../../testing/live-instance-status-fixtures';
import type { LiveInstanceActivityProjection } from '../reused/bot-trade-chart-card/bot-trade-chart-card.types';
import {
  activityProjectionForDisplay,
  activityRefreshKeyForStatus,
  cachedActivityForRequest,
  openOrderClustersForProjection,
} from './activity-tab.component';

function controlPlane(
  state: OperatorSurfaceControlPlane['state'],
): OperatorSurfaceControlPlane {
  return {
    state,
    last_transition_ms: 0,
    last_success_ms: null,
    attempt: state === 'RETRYING' ? 1 : 5,
    daemon_boot_id: null,
    notice: null,
    runbook_slug: null,
  };
}

function status(
  controlPlaneState: OperatorSurfaceControlPlane['state'] | null,
): LiveInstanceStatus {
  return {
    strategy_instance_id: 'sid-x',
    process: { state: 'running', pid: 1, bound_run_id: 'run-1', started_at_ms: 0 },
    live_binding: { run_id: 'run-1', run_dir: null, source: 'registry' },
    evidence_binding: null,
    desired_state: {
      state: 'RUNNING',
      path_status: 'ok',
      updated_at_ms: 0,
      updated_by: 'operator',
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
    symbol: 'DIA',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
    operator_surface: {
      schema_version: 1,
      host_process: {
        state: 'RUNNING',
        notice: null,
        copyable_command: null,
        last_exit_error_code: null,
        last_exit_error_message: null,
        last_exit_error_detail: {},
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'ALREADY_RUNNING',
          gate_results: [],
        },
      },
      prior_run: { classification: 'UNKNOWN' },
      broker: {
        safety_verdict: 'PAPER_ONLY',
        connection: 'CONNECTED',
        connection_condition: {
          code: 'BROKER_CONNECTED',
          severity: 'ok',
          title: 'Broker session connected',
          summary: 'The runtime has fresh proof that the IBKR broker session is connected.',
          remediation: null,
        },
      },
      configuration: { verdict: 'READY', reason_codes: [] },
      current_risk: {
        posture: 'FLAT',
        owned_positions: {},
        pending_order_count: 0,
        verdict: 'READY',
        unrealized_pnl: null,
      },
      daily_order_cap: { used: null, limit: null },
      action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
      account_owner: null,
      submit_readiness: {
        code: 'safe_to_monitor',
        label: 'Safe to monitor',
        explanation: 'Bot Control can observe this bot, but order submission is not currently active or appropriate.',
        can_submit: false,
        blocking_reason_codes: [],
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
        proof_lines: [
          {
            id: 'broker-proof',
            label: 'Broker',
            message: 'Paper broker is connected.',
            detail: 'Paper-only account proof is present. Broker session is connected.',
            tone: 'ok',
          },
          {
            id: 'submit-readiness',
            label: 'Trade submit',
            message: 'Safe to monitor',
            detail: 'Bot Control can observe this bot, but order submission is not currently active or appropriate.',
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
        advanced_evidence: [],
        template_id: 'operator_surface.trader_guidance.monitor_only',
        template_version: 1,
      },
      blockage_ladder: {
        headline: 'Lifecycle is clear',
        summary: 'No active blockage rung is currently limiting this bot.',
        current_stage_id: null,
        stages: [
          {
            id: 'broker',
            label: 'Broker proof',
            state: 'clear',
            severity: 'ok',
            current: false,
            title: 'Broker proof is clear',
            summary: 'Broker safety, connection, and submit capability have no active blockage findings.',
            next_step: null,
            reason_codes: [],
          },
        ],
      },
      run_signal: {
        state_label: 'On',
        tone: 'on',
        title: 'Bot process is running',
        detail: 'The host daemon reports this bot process is running.',
      },
      actions: {
        resume: {
          enabled: true,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
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
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [],
        },
      },
      trading_session: {
        phase: 'RTH',
        permits_strategy_activity: true,
        next_transition_ms: null,
        timezone: 'America/New_York',
        as_of_ms: 0,
      },
      readiness_gates: [],
      runtime_freshness: null,
      control_plane: controlPlaneState === null ? null : controlPlane(controlPlaneState),
      broker_observation_consistency: null,
      reconciliation: null,
      broker_activity_health: null,
      incident_headline: null,
    },
    lifecycle_chart: makeLifecycleChartFixture(),
    fetched_at_ms: 123_456,
  };
}

function activityProjection(overrides: Partial<LiveInstanceActivityProjection> = {}): LiveInstanceActivityProjection {
  return {
    schema_version: 1,
    strategy_instance_id: 'sid-a',
    session_date: '2026-06-29',
    timezone: 'America/New_York',
    symbol: 'SPY',
    resolution: '1m',
    has_bars: true,
    now_ms: 1_700_000_000_000,
    bars: [],
    fill_markers: [],
    position_annotations: [],
    order_overlays: [],
    orders_today: [],
    broker_activity_rows: [],
    position_snapshot: [],
    reconciliation_warnings: [],
    evidence: [],
    ...overrides,
  };
}

describe('activityRefreshKeyForStatus', () => {
  it('keeps refreshing while control plane is connected or actively retrying', () => {
    expect(activityRefreshKeyForStatus(status('CONNECTED'))).toBe(123_456);
    expect(activityRefreshKeyForStatus(status('RETRYING'))).toBe(123_456);
  });

  it('stops polling the activity projection after control-plane retry budget is terminal', () => {
    expect(activityRefreshKeyForStatus(status('UNREACHABLE'))).toBeNull();
    expect(activityRefreshKeyForStatus(status('AUTH_FAILED'))).toBeNull();
    expect(activityRefreshKeyForStatus(status('PROTOCOL_ERROR'))).toBeNull();
    expect(activityRefreshKeyForStatus(status('INCOMPATIBLE_CONTRACT'))).toBeNull();
  });
});

describe('cachedActivityForRequest', () => {
  it('reuses terminal-state activity only for the same sid, session date, and resolution', () => {
    const cached = {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m' as const,
      projection: null,
    };

    expect(cachedActivityForRequest(cached, {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m',
      refreshKey: null,
    })).toBeNull();
    expect(cachedActivityForRequest(cached, {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '5s',
      refreshKey: null,
    })).toBeUndefined();
    expect(cachedActivityForRequest(null, {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m',
      refreshKey: null,
    })).toBeUndefined();
  });
});

describe('activityProjectionForDisplay', () => {
  it('keeps the last matching projection visible while a background refresh is loading', () => {
    const cachedProjection = activityProjection({
      orders_today: [
        {
          order_key: 'perm:1',
          symbol: 'SPY',
          side: 'BUY',
          quantity: 1,
          order_type: 'MKT',
          status: 'filled',
          group: 'resolved',
          chart_ts_ms: 1_700_000_000_000,
          submitted_ts_ms: 1_700_000_000_000,
          last_update_ts_ms: 1_700_000_001_000,
          filled_quantity: 1,
          avg_fill_price: 420,
          position_effect: 'Open long',
          replay_count: 1,
          evidence: [],
        },
      ],
    });
    const request = {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m' as const,
      refreshKey: 123_456,
    };

    expect(activityProjectionForDisplay(undefined, {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m',
      projection: cachedProjection,
    }, request)).toBe(cachedProjection);
  });

  it('prefers the active resource value when a refresh completes', () => {
    const cachedProjection = activityProjection({ now_ms: 1 });
    const nextProjection = activityProjection({ now_ms: 2 });

    expect(activityProjectionForDisplay(nextProjection, {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m',
      projection: cachedProjection,
    }, {
      sid: 'sid-a',
      sessionDate: '2026-06-29',
      resolution: '1m',
      refreshKey: 123_456,
    })).toBe(nextProjection);
  });
});

describe('openOrderClustersForProjection', () => {
  it('keeps only working/pending order clusters so resolved outcomes do not duplicate the stream tail', () => {
    const projection = activityProjection({
      orders_today: [
        {
          order_key: 'active',
          symbol: 'SPY',
          side: 'BUY',
          quantity: 1,
          order_type: 'MKT',
          status: 'submitted',
          group: 'active',
          chart_ts_ms: 1_700_000_000_000,
          submitted_ts_ms: 1_700_000_000_000,
          last_update_ts_ms: 1_700_000_001_000,
          filled_quantity: 0,
          avg_fill_price: null,
          position_effect: null,
          replay_count: 1,
          evidence: [],
        },
        {
          order_key: 'pending',
          symbol: 'SPY',
          side: 'SELL',
          quantity: 1,
          order_type: 'MKT',
          status: 'engine pending',
          group: 'engine_pending',
          chart_ts_ms: 1_700_000_002_000,
          submitted_ts_ms: 1_700_000_002_000,
          last_update_ts_ms: 1_700_000_002_000,
          filled_quantity: 0,
          avg_fill_price: null,
          position_effect: null,
          replay_count: 1,
          evidence: [],
        },
        {
          order_key: 'resolved',
          symbol: 'SPY',
          side: 'BUY',
          quantity: 1,
          order_type: 'MKT',
          status: 'filled',
          group: 'resolved',
          chart_ts_ms: 1_700_000_003_000,
          submitted_ts_ms: 1_700_000_003_000,
          last_update_ts_ms: 1_700_000_004_000,
          filled_quantity: 1,
          avg_fill_price: 420,
          position_effect: 'Open long',
          replay_count: 1,
          evidence: [],
        },
      ],
    });

    expect(openOrderClustersForProjection(projection).map((row) => row.order_key))
      .toEqual(['active', 'pending']);
    expect(openOrderClustersForProjection(null)).toEqual([]);
  });
});
