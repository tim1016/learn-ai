import { Component, signal } from '@angular/core';
import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { convertToParamMap, ActivatedRoute, provideRouter } from '@angular/router';
import { of, Subject } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { FleetAccountSummary, LiveInstanceStatus } from '../../../api/live-instances.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerBannerComponent } from '../../../shell/broker-banner.component';
import { makeLifecycleChartFixture } from '../../../testing/live-instance-status-fixtures';
import { BotControlPageComponent } from './bot-control-page.component';

@Component({
  imports: [BotControlPageComponent, BrokerBannerComponent],
  template: `
    <app-bot-control-page />
    <app-broker-banner />
  `,
})
class BotControlWithSidebarHostComponent {}

class FakeBrokerHealthService {
  readonly health = signal(null);
  readonly bannerState = signal(null);
  readonly lifecycleAction = signal(null);
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
}

function makeStatus(options: {
  id?: string;
  hostNotice?: string;
  markPoisonedEnabled?: boolean;
} = {}): LiveInstanceStatus {
  return {
    strategy_instance_id: options.id ?? 'sid-x',
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
        notice: options.hostNotice ?? 'Start the host runner before trading this bot.',
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
          enabled: options.markPoisonedEnabled ?? false,
          effect: 'LIVE_ACTUATION',
          disabled_reason_code: options.markPoisonedEnabled ? null : 'NO_LIVE_BINDING',
          disabled_reasons: options.markPoisonedEnabled ? [] : ['NO_LIVE_BINDING'],
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
        runbook_slug: 'control plane/runbook?',
      },
      broker_observation_consistency: null,
      reconciliation: null,
      broker_activity_health: null,
      incident_headline: null,
    },
    lifecycle_chart: makeLifecycleChartFixture(),
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
      runbook_slug: 'broker evidence/health?',
      occurred_at_ms: null,
    },
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flush(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }): Promise<void> {
  await fixture.whenStable();
  await Promise.resolve();
  fixture.detectChanges();
}

describe('BotControlPageComponent', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders compact broker evidence and control-plane warning panels before the bot tabs', async () => {
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
            startHostRunner: vi.fn(),
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
    const brokerPanel = el.querySelector('[data-testid="bot-control-broker-evidence-banner"]');
    const controlPanel = el.querySelector('[data-testid="bot-control-plane-banner"]');
    expect(brokerPanel?.textContent)
      .toContain('Warning, broker evidence unavailable.');
    expect(controlPanel?.textContent)
      .toContain('Control plane, last known.');
    const runbookLinks = Array.from(el.querySelectorAll<HTMLAnchorElement>('.warning-link'))
      .map((link) => link.getAttribute('href'));
    expect(runbookLinks).toContain('/runbooks/broker%20evidence%2Fhealth%3F');
    expect(runbookLinks).toContain('/runbooks/control%20plane%2Frunbook%3F');
    expect(el.querySelector('[data-testid="bot-control-host-runner-banner"]')).toBeNull();
    expect(el.querySelector('[data-testid="bot-control-tabs"]')?.textContent)
      .toContain('Status & Risk');
  });

  it('renders the active bot host-runner warning through the sidebar consumer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      imports: [BotControlWithSidebarHostComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            startHostRunner: vi.fn(),
            setInstanceDesiredState: vi.fn(),
            flattenAndPause: vi.fn(),
            issueInstanceCommand: vi.fn(),
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlWithSidebarHostComponent);
    fixture.detectChanges();
    await flush(fixture);

    const el = fixture.nativeElement as HTMLElement;
    const sidebarNotice = el.querySelector('[data-testid="sidebar-host-runner-notice"]');
    expect(sidebarNotice?.textContent).toContain('Start the host runner before trading this bot.');
    expect(sidebarNotice?.textContent).toContain('make broker-runner');
    expect(el.querySelector('[data-testid="bot-control-host-runner-banner"]')).toBeNull();
  });

  it('refreshes broker evidence on the serialized poll loop', async () => {
    vi.useFakeTimers();
    const getInstanceStatus = vi.fn().mockResolvedValue(makeStatus());
    const getAccountSummary = vi.fn().mockResolvedValue(makeAccountSummary());
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
            getInstanceStatus,
            getAccountSummary,
            startHostRunner: vi.fn(),
            setInstanceDesiredState: vi.fn(),
            flattenAndPause: vi.fn(),
            issueInstanceCommand: vi.fn(),
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlPageComponent);
    fixture.detectChanges();
    await flush(fixture);
    expect(getAccountSummary).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(4_000);
    await flush(fixture);

    expect(getInstanceStatus).toHaveBeenCalledTimes(2);
    expect(getAccountSummary).toHaveBeenCalledTimes(2);
  });

  it('ignores stale status responses after the route changes to another bot', async () => {
    const paramMap = new Subject<ReturnType<typeof convertToParamMap>>();
    const first = deferred<LiveInstanceStatus>();
    const second = deferred<LiveInstanceStatus>();
    const getInstanceStatus = vi
      .fn()
      .mockImplementation((id: string) => id === 'bot-a' ? first.promise : second.promise);
    TestBed.configureTestingModule({
      imports: [BotControlWithSidebarHostComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        {
          provide: ActivatedRoute,
          useValue: { paramMap },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus,
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            startHostRunner: vi.fn(),
            setInstanceDesiredState: vi.fn(),
            flattenAndPause: vi.fn(),
            issueInstanceCommand: vi.fn(),
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlWithSidebarHostComponent);
    fixture.detectChanges();
    paramMap.next(convertToParamMap({ id: 'bot-a' }));
    paramMap.next(convertToParamMap({ id: 'bot-b' }));
    second.resolve(makeStatus({ id: 'bot-b', hostNotice: 'B runner is unreachable.' }));
    await flush(fixture);
    first.resolve(makeStatus({ id: 'bot-a', hostNotice: 'A runner is unreachable.' }));
    await flush(fixture);

    const sidebarNotice = (fixture.nativeElement as HTMLElement)
      .querySelector('[data-testid="sidebar-host-runner-notice"]');
    expect(sidebarNotice?.textContent).toContain('B runner is unreachable.');
  });

  it('requires typed HALT before marking a run poisoned', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const issueInstanceCommand = vi.fn().mockResolvedValue({});
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
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus({ markPoisonedEnabled: true })),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            startHostRunner: vi.fn(),
            setInstanceDesiredState: vi.fn(),
            flattenAndPause: vi.fn(),
            issueInstanceCommand,
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlPageComponent);
    fixture.detectChanges();
    await flush(fixture);
    const el = fixture.nativeElement as HTMLElement;
    fixture.componentInstance.openTypedHalt();
    fixture.detectChanges();

    const submit = el.querySelector('[data-testid="typed-halt-confirm-submit"]') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    const input = el.querySelector('[data-testid="typed-halt-confirm-input"]') as HTMLInputElement;
    input.value = 'HALT';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    submit.click();
    await flush(fixture);

    expect(issueInstanceCommand).toHaveBeenCalledWith('sid-x', { verb: 'MARK_POISONED' });
  });
});
