import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';

import type { GateResult, LiveInstanceStatus } from '../../../../api/live-instances.types';
import { makeLifecycleChartFixture } from '../../../../testing/live-instance-status-fixtures';
import { ConfigurationTabComponent } from './configuration-tab.component';

function gateResult(gateId: string, status: GateResult['status']): GateResult {
  return {
    gate_id: gateId,
    status,
    source: 'fixture',
    operator_reason: status === 'pass' ? 'GATE_PASSING' : 'GATE_BLOCKING',
    operator_next_step: status === 'pass' ? null : 'Review the blocking gate.',
    evidence_at_ms: 0,
  };
}

function status(): LiveInstanceStatus {
  return {
    strategy_instance_id: 'sid-x',
    process: { state: 'idle', bound_run_id: null, pid: null, started_at_ms: null },
    live_binding: null,
    evidence_binding: null,
    desired_state: null,
    readiness: null,
    latest_decision: null,
    decision_columns: [],
    broker: null,
    start_defaults: {
      strategy: 'SPY EMA crossover',
      readonly: false,
      hydrate_policy: 'require',
      max_orders_per_day: 3,
      ibkr_host: '127.0.0.1',
    },
    provenance: {
      run_id: 'run-1',
      schema_version: '1',
      code_sha: 'abc123',
      strategy_spec_path: 'PythonDataService/app/engine/strategy/spec/fixtures/spy.spec.json',
      strategy_spec_sha256: 'spec-sha',
      qc_audit_copy_path: 'references/qc-shadow/spy.py',
      qc_audit_copy_sha256: 'qc-sha',
      qc_cloud_backtest_id: 'qc-1',
      account_id: 'DU123',
      start_date_ms: null,
      created_at_ms: null,
      live_config: {},
    },
    sizing: {
      policy: { kind: 'FixedShares', value: 1 },
      preset: 'safe_canary',
      governed_by: 'live_config',
      sizing_provenance: 'live_override',
      per_trade_audit: [],
    },
    last_exit: null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: 'explicit',
    lineage: {
      parent_run_id: 'run-parent',
      redeploy_reason: 'operator_recovery',
      redeployed_at_ms: 1_700_000_000_000,
    },
    operator_surface: {
      schema_version: 1,
      host_process: {
        state: 'IDLE',
        notice: null,
        copyable_command: null,
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'STOPPED_REQUIRES_REDEPLOY',
          gate_results: [gateResult('host_process.start', 'block')],
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
      daily_order_cap: { used: 1, limit: 3 },
      action_plan: { consumption: 'UNKNOWN', anomaly_verdict: 'UNKNOWN' },
      account_owner: null,
      submit_readiness: {
        code: 'safe_to_monitor',
        label: 'Safe to monitor',
        explanation: 'The cockpit can observe this bot, but order submission is not currently active or appropriate.',
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
        advanced_evidence: [],
        template_id: 'operator_surface.trader_guidance.monitor_only',
        template_version: 1,
      },
      actions: {
        resume: {
          enabled: false,
          effect: 'DURABLE_ONLY',
          disabled_reason_code: 'REDEPLOY_REQUIRED',
          disabled_reasons: ['REDEPLOY_REQUIRED'],
          gate_results: [gateResult('action.resume', 'block')],
        },
        pause: {
          enabled: true,
          effect: 'DURABLE_ONLY',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [gateResult('action.pause', 'pass')],
        },
        stop: {
          enabled: true,
          effect: 'DURABLE_ONLY',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [gateResult('action.stop', 'pass')],
        },
        flatten_and_pause: {
          enabled: false,
          effect: 'DURABLE_ONLY',
          disabled_reason_code: 'NO_OWNED_POSITIONS',
          disabled_reasons: ['NO_OWNED_POSITIONS'],
          gate_results: [gateResult('action.flatten_and_pause', 'block')],
        },
        mark_poisoned: {
          enabled: true,
          effect: 'DURABLE_ONLY',
          disabled_reason_code: null,
          disabled_reasons: [],
          gate_results: [gateResult('action.mark_poisoned', 'pass')],
        },
      },
      trading_session: {
        phase: 'RTH',
        permits_strategy_activity: true,
        next_transition_ms: null,
        timezone: 'America/New_York',
        as_of_ms: 1_700_000_000_000,
      },
      readiness_gates: [],
      runtime_freshness: null,
      control_plane: null,
      broker_observation_consistency: null,
      reconciliation: null,
      broker_activity_health: null,
      incident_headline: null,
    },
    lifecycle_chart: makeLifecycleChartFixture(),
    fetched_at_ms: 1_700_000_000_000,
  };
}

function render() {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection(), provideRouter([])],
  });
  const fixture = TestBed.createComponent(ConfigurationTabComponent);
  fixture.componentRef.setInput('status', status());
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('ConfigurationTabComponent', () => {
  it('keeps raw implementation fields out of the primary configuration labels', () => {
    const el = render();
    const deployment = el.querySelector('[data-testid="configuration-deployment"]');
    const primaryText = deployment?.querySelector('dl')?.textContent ?? '';

    expect(primaryText).toContain('Strategy');
    expect(primaryText).toContain('Broker account');
    expect(primaryText).toContain('Daily order limit');
    expect(primaryText).not.toContain('strategy_key');
    expect(primaryText).not.toContain('spec_path');
    expect(primaryText).not.toContain('max_orders_per_day');
    expect(el.textContent ?? '').toContain('Technical deployment evidence');
    expect(el.textContent ?? '').toContain('Technical sizing policy');
  });
});
