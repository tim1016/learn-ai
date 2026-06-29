import { describe, expect, it } from 'vitest';

import type { LiveInstanceStatus, OperatorSurfaceControlPlane } from '../../../../api/live-instances.types';
import { makeLifecycleChartFixture } from '../../../../testing/live-instance-status-fixtures';
import { activityRefreshKeyForStatus, cachedActivityForRequest } from './activity-tab.component';

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
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'ALREADY_RUNNING',
          gate_results: [],
        },
      },
      prior_run: { classification: 'UNKNOWN' },
      broker: { safety_verdict: 'PAPER_ONLY', connection: 'CONNECTED' },
      configuration: { verdict: 'READY', reason_codes: [] },
      current_risk: {
        posture: 'FLAT',
        pending_order_count: 0,
        verdict: 'READY',
        unrealized_pnl: null,
      },
      daily_order_cap: { used: null, limit: null },
      action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
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
