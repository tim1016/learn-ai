import { Component, signal } from '@angular/core';
import { provideZonelessChangeDetection } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpErrorResponse } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { convertToParamMap, ActivatedRoute, provideRouter } from '@angular/router';
import { of, Subject } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  FleetAccountSummary,
  LifecycleTimelineResponse,
  LiveInstanceStatus,
} from '../../../api/live-instances.types';
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

function makeLifecycleTimeline(): LifecycleTimelineResponse {
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
        summary: 'Broker acknowledgement failed; submit outcome is uncertain.',
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

function installLocalStorageStub(): void {
  const store = new Map<string, string>();
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => store.set(key, value),
      removeItem: (key: string) => store.delete(key),
      clear: () => store.clear(),
    },
  });
}

describe('BotControlPageComponent', () => {
  beforeEach(() => {
    installLocalStorageStub();
  });

  afterEach(() => {
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it('renders compact broker evidence and control-plane warning panels before the bot tabs', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
    expect(runbookLinks).toContain('/docs/signal-engine-methodology#broker%20evidence/health?');
    expect(runbookLinks).toContain('/docs/signal-engine-methodology#control%20plane/runbook?');
    expect(el.querySelector('[data-testid="bot-control-host-runner-banner"]')).toBeNull();
    expect(el.querySelector('[data-testid="bot-control-tabs"]')).toBeNull();
    expect(el.querySelector('.decision-row')?.textContent).toContain('Broker proof');
  });

  it('persists the attention panel collapsed per bot and situation code', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const liveRuns = {
      getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
      getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
      getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
      startHostRunner: vi.fn(),
      setInstanceDesiredState: vi.fn(),
      flattenAndPause: vi.fn(),
      issueInstanceCommand: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: liveRuns,
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlPageComponent);
    fixture.detectChanges();
    await flush(fixture);

    const panel = (fixture.nativeElement as HTMLElement)
      .querySelector<HTMLDetailsElement>('[data-testid="bot-control-attention-panel"]');
    expect(panel?.open).toBe(true);
    if (!panel) throw new Error('Expected attention panel.');
    panel.open = false;
    panel.dispatchEvent(new Event('toggle'));
    fixture.detectChanges();
    expect(window.localStorage.getItem('bot-control-attention:sid-x:broker_state_unproven'))
      .toBe('closed');
    fixture.destroy();

    const second = TestBed.createComponent(BotControlPageComponent);
    second.detectChanges();
    await flush(second);

    const reopened = (second.nativeElement as HTMLElement)
      .querySelector<HTMLDetailsElement>('[data-testid="bot-control-attention-panel"]');
    expect(reopened?.open).toBe(false);
  });

  it('keeps lifecycle overview visible and switches the right pane from selected chart nodes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('.top-action-banner')?.textContent).toContain('Act now');
    const startAction = el.querySelector(
      '.top-action-banner .chart-action[aria-label="Start bot process"]',
    ) as HTMLButtonElement | null;
    expect(startAction).not.toBeNull();
    expect(startAction?.textContent?.trim()).toBe('');
    expect(el.querySelector('app-overview-tab')).not.toBeNull();
    expect(el.querySelector('app-overview-tab app-trader-guidance-pane')).toBeNull();
    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Current lifecycle focus');
    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Deploy or start');

    const dispatch = vi.spyOn(fixture.componentInstance, 'dispatchOverviewAction');
    startAction?.click();
    expect(dispatch).toHaveBeenCalledWith('start_process');

    const recovery = fixture.componentInstance.status()
      ?.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    expect(recovery).toBeDefined();
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    fixture.componentInstance.selectLifecycleNode(recovery);
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Selected lifecycle step');
    expect(el.querySelector('[data-testid="bot-control-context-header"]')?.textContent)
      .toContain('Recovery lane');
    expect(el.querySelector('[data-testid="bot-control-tabs"]')).toBeNull();
  });

  it('renders the Execution chip only from backend-authored execution posture', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.execution = { posture: 'UNSAFE' };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const chip = (fixture.nativeElement as HTMLElement)
      .querySelector('[data-testid="bot-control-execution-chip"]');
    expect(chip?.textContent).toContain('Execution:');
    expect(chip?.textContent).toContain('UNSAFE');
  });

  it('does not render an Execution chip when the backend omits execution posture', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    expect((fixture.nativeElement as HTMLElement)
      .querySelector('[data-testid="bot-control-execution-chip"]')).toBeNull();
  });

  it('renders backend-authored disabled action prose only in the disabled tooltip', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.lifecycle_chart.actions = [
      {
        id: 'flatten_and_pause',
        label: 'Flatten and pause',
        enabled: false,
        reason_code: 'NO_LIVE_BINDING',
        reason_headline: 'No live binding',
        reason_detail: 'The lifecycle action contract says the runner is not bound.',
        target_node_id: 'recovery',
        tone: 'danger',
      },
    ];
    status.operator_surface.actions.flatten_and_pause = {
      enabled: false,
      effect: 'LIVE_ACTUATION',
      disabled_reason_code: 'BROKER_SAFETY_UNSAFE',
      disabled_reasons: ['BROKER_SAFETY_UNSAFE'],
      gate_results: [],
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const el = fixture.nativeElement as HTMLElement;
    const actionButton = el.querySelector<HTMLButtonElement>('[aria-label="Flatten and pause"]');
    expect(actionButton?.getAttribute('title')).toContain('No live binding');
    expect(actionButton?.getAttribute('title')).toContain(
      'The lifecycle action contract says the runner is not bound.',
    );
    expect(actionButton?.getAttribute('title')).not.toContain('NO_LIVE_BINDING');
    const traderCopy = Array.from(el.querySelectorAll('[data-trader-copy]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    const receipts = Array.from(el.querySelectorAll('[data-receipt]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    expect(traderCopy).not.toContain('NO_LIVE_BINDING');
    expect(traderCopy).not.toContain('BROKER_SAFETY_UNSAFE');
    expect(receipts).not.toContain('NO_LIVE_BINDING');
    expect(receipts).not.toContain('BROKER_SAFETY_UNSAFE');
  });

  it('keeps node selection explanatory and never gates enabled emergency actions', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.lifecycle_chart.actions = [
      {
        id: 'pause',
        label: 'Pause',
        enabled: true,
        reason_code: null,
        reason_headline: 'Available',
        reason_detail: 'Backend gates currently allow this action.',
        target_node_id: 'active',
        tone: 'secondary',
      },
    ];
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const recovery = status.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    fixture.componentInstance.selectLifecycleNode(recovery);
    fixture.detectChanges();

    const pause = (fixture.nativeElement as HTMLElement)
      .querySelector<HTMLButtonElement>('.chart-action[aria-label="Pause"]');
    expect(pause?.getAttribute('aria-disabled')).toBe('false');
    expect(pause?.getAttribute('title')).toBeNull();
    expect(pause?.textContent?.trim()).toBe('');
  });

  it('renders unknown deploy order mode as not recorded', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const deployConfig = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('[data-testid="redeploy-setting-field"]'),
    ).find((field) => field.textContent?.includes('Deploy/start config'));
    expect(deployConfig?.textContent).toContain('Order mode: Not recorded.');
    expect(deployConfig?.textContent).not.toContain('Order placement allowed');
  });

  it('does not label stale runtime receipts as fresh evidence', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.runtime_freshness = {
      posture_demoted: false,
      stale_reason_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
      command_loop: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
      broker: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
      bar_loop: { state: 'STALE', age_ms: 90_000, stale_reason_codes: ['BAR_LOOP_LATEST_BAR_STALE'] },
      control_plane: { state: 'FRESH', age_ms: 100, stale_reason_codes: [] },
      headline: null,
      additional_reasons: [],
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const runtimeField = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('[data-testid="locked-evidence-field"]'),
    ).find((field) => field.textContent?.includes('Runtime freshness'));
    expect(runtimeField?.textContent).toContain('ATTENTION');
    expect(runtimeField?.textContent).not.toContain('FRESH');
    expect(runtimeField?.querySelector('[data-receipt]')?.textContent)
      .toContain('Bar Loop Latest Bar Stale');
  });

  it('renders the projection timeline below the fold as recent activity', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    status.operator_surface.account_owner = {
      account_id: 'DU1',
      generation: 4,
      phase: 'accepting',
      recorded_at_ms: 1_700_000_000_000,
      source: 'account_owner',
    };
    const getLifecycleTimeline = vi.fn().mockResolvedValue(makeLifecycleTimeline());
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline,
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

    expect(getLifecycleTimeline).toHaveBeenCalledWith({
      account_id: 'DU1',
      strategy_instance_id: 'sid-x',
      run_id: null,
      limit: 5,
    });
    const timeline = (fixture.nativeElement as HTMLElement)
      .querySelector('[data-testid="bot-control-recent-activity"] [data-testid="trader-guidance-timeline"]');
    expect(timeline?.textContent).toContain('Broker acknowledgement failed; submit outcome is uncertain.');
    expect(timeline?.textContent).toContain('broker_ack #7');
  });

  it('clears lifecycle timeline rows when refreshed status changes run context', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const nextTimeline = deferred<LifecycleTimelineResponse>();
    const nextStatus = {
      ...makeStatus(),
      evidence_binding: { run_id: 'run-y', state: 'latest_run_by_ledger', is_live: false },
    };
    const getInstanceStatus = vi.fn()
      .mockResolvedValueOnce(makeStatus())
      .mockResolvedValueOnce(nextStatus);
    const getLifecycleTimeline = vi.fn()
      .mockResolvedValueOnce(makeLifecycleTimeline())
      .mockReturnValueOnce(nextTimeline.promise);
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus,
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline,
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
    expect((fixture.nativeElement as HTMLElement).textContent)
      .toContain('Broker acknowledgement failed; submit outcome is uncertain.');

    await (fixture.componentInstance as unknown as { refreshStatus(id: string): Promise<void> }).refreshStatus('sid-x');
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).not.toContain('Broker acknowledgement failed; submit outcome is uncertain.');
    expect(text).toContain('Lifecycle projection is unavailable for this bot.');
  });

  it('renders selected lifecycle node freshness and receipts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    const reconcile = status.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'reconcile');
    if (!reconcile) throw new Error('Expected reconcile lifecycle node in fixture.');
    reconcile.ts_ms = 1_700_000_001_000;
    reconcile.ts_ms_resolved = true;
    reconcile.receipts = [
      {
        label: 'reconciliation.state',
        value: 'FAILED',
        unit: null,
        source: 'reconciliation_projection',
        gate_id: null,
        ts_ms: 1_700_000_001_000,
        ts_ms_resolved: true,
      },
      {
        label: 'failure_reason',
        value: 'Broker snapshot disagrees with the intent WAL.',
        unit: null,
        source: 'reconciliation_projection',
        gate_id: null,
        ts_ms: null,
        ts_ms_resolved: false,
      },
    ];
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
    fixture.componentInstance.selectLifecycleNode(reconcile);
    fixture.detectChanges();

    const receipts = (fixture.nativeElement as HTMLElement).querySelector('[data-testid="bot-control-node-receipts"]');
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Evidence time:');
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('ET');
    expect(receipts?.textContent).toContain('Reconciliation State');
    expect(receipts?.textContent).toContain('Failed');
    expect(receipts?.textContent).toContain('Failure Reason');
    expect(receipts?.textContent).toContain('Broker snapshot disagrees with the intent WAL.');
    expect(receipts?.textContent).toContain('Reconciliation Projection');
  });

  it('keeps lifecycle node codes out of trader copy and formats them in receipts', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const status = makeStatus();
    const hostState = status.lifecycle_chart.subgraphs['deploy'].nodes.find(
      (node) => node.id === 'host_state',
    );
    if (!hostState) throw new Error('Expected host-state lifecycle node in fixture.');
    hostState.summary = 'Host state requires one backend receipt before this run is ready.';
    hostState.evidence_summary = hostState.summary;
    hostState.receipts = [
      {
        label: 'host_process.disabled_reason_code',
        value: 'HOST_SERVICE_OFFLINE',
        unit: null,
        source: 'operator_surface.host_process',
        gate_id: null,
        ts_ms: null,
        ts_ms_resolved: false,
      },
    ];
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(status),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
    fixture.componentInstance.selectLifecycleNode(hostState);
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    const traderCopy = Array.from(el.querySelectorAll('[data-trader-copy]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    const receipts = Array.from(el.querySelectorAll('[data-receipt]'))
      .map((node) => node.textContent ?? '')
      .join(' ');
    expect(traderCopy).toContain('Host state requires one backend receipt');
    expect(traderCopy).not.toContain('HOST_SERVICE_OFFLINE');
    expect(receipts).toContain('Host Service Offline');
  });

  it('keeps the cockpit file-backed when the projection timeline is unavailable', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getLifecycleTimeline = vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 503 }));
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline,
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

    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('app-overview-tab')).not.toBeNull();
    expect(el.querySelector('[data-testid="bot-control-recent-activity"]')?.textContent)
      .toContain('Projection unavailable; current snapshot remains file-backed.');
    expect(el.querySelector('.error-banner')?.textContent ?? '').not.toContain('Projection unavailable');
  });

  it('routes the trader guidance reconcile action to the existing instance endpoint', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reconcileInstance = vi.fn().mockResolvedValue({});
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus()),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
            startHostRunner: vi.fn(),
            setInstanceDesiredState: vi.fn(),
            flattenAndPause: vi.fn(),
            issueInstanceCommand: vi.fn(),
            reconcileInstance,
          },
        },
      ],
    });

    const fixture = TestBed.createComponent(BotControlPageComponent);
    fixture.detectChanges();
    await flush(fixture);

    const action = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="trader-guidance-primary-remediation"]',
    ) as HTMLButtonElement | null;
    expect(action?.textContent).toContain('Reconcile now');
    action?.click();
    await flush(fixture);

    expect(reconcileInstance).toHaveBeenCalledWith('sid-x');
  });

  it('re-derives selected lifecycle context from refreshed status data', async () => {
    const firstStatus = makeStatus();
    const secondStatus = makeStatus();
    const secondRecovery = secondStatus.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!secondRecovery) throw new Error('Expected recovery lifecycle node in fixture.');
    secondRecovery.status_label = 'Updated by poll';
    secondRecovery.evidence_summary = 'Recovery evidence refreshed.';

    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(firstStatus),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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

    const recovery = fixture.componentInstance.status()
      ?.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    fixture.componentInstance.selectLifecycleNode(recovery);
    fixture.detectChanges();

    fixture.componentInstance.status.set(secondStatus);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Updated by poll');
    expect(text).toContain('Recovery evidence refreshed.');
  });

  it('resets selected tab, lifecycle context, and typed HALT when the route changes to another bot', async () => {
    const paramMap = new Subject<ReturnType<typeof convertToParamMap>>();
    const issueInstanceCommand = vi.fn().mockResolvedValue({});
    const getInstanceStatus = vi.fn().mockResolvedValue(makeStatus({ markPoisonedEnabled: true }));
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus,
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
    paramMap.next(convertToParamMap({ id: 'bot-a' }));
    await flush(fixture);

    const recovery = fixture.componentInstance.status()
      ?.lifecycle_chart.global_graph.nodes.find((node) => node.id === 'recovery');
    if (!recovery) throw new Error('Expected recovery lifecycle node in fixture.');
    fixture.componentInstance.selectLifecycleNode(recovery);
    fixture.componentInstance.openTypedHalt();
    fixture.detectChanges();
    expect(fixture.componentInstance.selectedLifecycleNodeId()).toBe('recovery');
    expect(fixture.componentInstance.typedHaltOpen()).toBe(true);
    expect((fixture.nativeElement as HTMLElement).querySelector('[data-testid="bot-control-tabs"]')).toBeNull();

    paramMap.next(convertToParamMap({ id: 'bot-b' }));
    await flush(fixture);

    expect(fixture.componentInstance.selectedLifecycleNodeId()).toBeNull();
    expect(fixture.componentInstance.typedHaltOpen()).toBe(false);
    await fixture.componentInstance.confirmTypedHalt();
    expect(issueInstanceCommand).not.toHaveBeenCalled();
  });

  it('renders the active bot host-runner warning through the sidebar consumer', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    TestBed.configureTestingModule({
      imports: [BotControlWithSidebarHostComponent],
      providers: [
        provideZonelessChangeDetection(),
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
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
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus,
            getAccountSummary,
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
    expect(getAccountSummary).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(4_000);
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

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
        provideHttpClient(),
        provideHttpClientTesting(),
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
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
        provideHttpClient(),
        provideHttpClientTesting(),
        {
          provide: ActivatedRoute,
          useValue: { paramMap: of(convertToParamMap({ id: 'sid-x' })) },
        },
        {
          provide: LiveRunsService,
          useValue: {
            getInstanceStatus: vi.fn().mockResolvedValue(makeStatus({ markPoisonedEnabled: true })),
            getAccountSummary: vi.fn().mockResolvedValue(makeAccountSummary()),
            getLifecycleTimeline: vi.fn().mockResolvedValue(makeLifecycleTimeline()),
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
