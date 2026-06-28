import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { convertToParamMap, ActivatedRoute, provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { FleetAccountSummary, LiveInstanceStatus } from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BotControlPageComponent } from './bot-control-page.component';

function makeStatus(): LiveInstanceStatus {
  return {
    strategy_instance_id: 'sid-x',
    process: { state: 'exited', pid: null, bound_run_id: null, started_at_ms: null },
    live_binding: null,
    evidence_binding: null,
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
        state: 'UNREACHABLE',
        notice: 'Start the host runner before trading this bot.',
        copyable_command: 'make broker-runner',
        start_capability: {
          enabled: false,
          run_id: null,
          request: null,
          disabled_reason_code: 'HOST_SERVICE_OFFLINE',
          gate_results: [],
        },
      },
      prior_run: { classification: 'UNKNOWN' },
      broker: { safety_verdict: 'UNKNOWN', connection: 'DISCONNECTED' },
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
          enabled: false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: 'NO_LIVE_BINDING',
          disabled_reasons: ['NO_LIVE_BINDING'],
          gate_results: [],
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
      runtime_freshness: null,
      control_plane: {
        state: 'UNREACHABLE',
        last_transition_ms: 0,
        last_success_ms: null,
        attempt: 0,
        daemon_boot_id: null,
        notice: 'Last command channel health check failed.',
        runbook_slug: 'control-plane',
      },
      broker_observation_consistency: null,
      reconciliation: null,
      broker_activity_health: null,
      incident_headline: null,
    },
    fetched_at_ms: 0,
  };
}

function makeAccountSummary(): FleetAccountSummary {
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
    notice: {
      code: 'activity.source_blind_to_bot_orders',
      tier: 'warning',
      title: 'Broker evidence is unavailable',
      message: 'The data plane could not fetch broker net positions.',
      source_codes: [],
      forensic_facts: {},
      action: {
        kind: 'external_manual_check',
        label: 'Check positions in IBKR',
        target: 'ibkr_positions',
      },
      runbook_slug: 'broker-evidence-health',
      occurred_at_ms: null,
    },
  };
}

describe('BotControlPageComponent', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders broker evidence, host runner, and control-plane banners before the bot tabs', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            setInstanceDesiredState: vi.fn(),
            flattenAndPause: vi.fn(),
            issueInstanceCommand: vi.fn(),
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlPageComponent);
    fixture.detectChanges();
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('[data-testid="bot-control-broker-evidence-banner"]')?.textContent)
      .toContain('Broker evidence is unavailable');
    expect(el.querySelector('[data-testid="bot-control-host-runner-banner"]')?.textContent)
      .toContain('Host runner unreachable');
    expect(el.querySelector('[data-testid="bot-control-plane-banner"]')?.textContent)
      .toContain('CONTROL PLANE · LAST-KNOWN');
    expect(el.querySelector('[data-testid="bot-control-tabs"]')?.textContent)
      .toContain('Status & Risk');
  });
});
