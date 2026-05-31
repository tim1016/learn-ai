import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  LiveInstanceStatus,
  LiveInstanceSummary,
} from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';
import { BrokerInstancesComponent } from './broker-instances.component';

const FLEET: LiveInstanceSummary[] = [
  {
    strategy_instance_id: 'spy_ema_paper',
    process_state: 'running',
    bound_run_id: 'run-live',
    latest_run_id: 'run-live',
  },
  {
    strategy_instance_id: 'spy_vwap_shadow',
    process_state: 'offline',
    bound_run_id: null,
    latest_run_id: 'run-old',
  },
];

function makeStatus(overrides: Partial<LiveInstanceStatus> = {}): LiveInstanceStatus {
  return {
    strategy_instance_id: 'spy_ema_paper',
    process: { state: 'running', pid: 99, bound_run_id: 'run-live', started_at_ms: 1 },
    live_binding: { run_id: 'run-live', run_dir: null, source: 'registry' },
    evidence_binding: { run_id: 'run-live', state: 'latest_run_by_ledger', is_live: false },
    desired_state: {
      state: 'RUNNING',
      updated_at_ms: 1,
      updated_by: 'operator',
      reason: null,
      version: 1,
      path_status: 'ok',
    },
    readiness: {
      kind: 'live_readiness',
      as_of_ms: 1,
      source: 'engine',
      verdict: 'BLOCKED',
      summary: 'Blocked: orders_cap — 4 / 4 orders used.',
      gates: [
        { name: 'orders_cap', status: 'fail', severity: 'hard', detail: '4 / 4 orders used' },
      ],
    },
    latest_decision: { signal: 'ENTER', ema5: 624.123, rsi: 61.2 },
    decision_columns: [
      { name: 'ema5', label: 'EMA 5', type: 'float64', format: 'decimal' },
      { name: 'rsi', label: 'RSI', type: 'float64', format: 'decimal' },
    ],
    broker: {
      bot_order_namespace: 'spy_ema_ns',
      owned_positions: { SPY: 100 },
      pending_order_count: 1,
    },
    start_defaults: {
      strategy: 'spy_ema_crossover',
      readonly: true,
      hydrate_policy: 'require',
      max_orders_per_day: 4,
      ibkr_host: '127.0.0.1',
    },
    fetched_at_ms: 1,
    ...overrides,
  };
}

class FakeLiveRunsService {
  getInstances = vi.fn().mockResolvedValue(FLEET);
  getInstanceStatus = vi.fn().mockResolvedValue(makeStatus());
  getAccountFleet = vi.fn().mockResolvedValue({
    net_positions: { SPY: 137 },
    explained_total: { SPY: 100 },
    explained_by_instance: [{ strategy_instance_id: 'spy_ema_paper', positions: { SPY: 100 } }],
    residual: { SPY: 37 },
    verdict: 'contaminated',
    policy_blocks_starts: false,
    summary: 'Account residual: SPY +37 unattributed outside managed namespaces.',
  });
  setInstanceDesiredState = vi.fn().mockResolvedValue({
    durable: { state: 'PAUSED', updated_at_ms: 1, updated_by: 'operator', reason: null, version: 2 },
    actuation: {
      actuated: true,
      run_id: 'run-live',
      command_seq: 1,
      detail: 'PAUSE queued on run-live; awaiting ack',
    },
  });
  getInstanceCommands = vi.fn().mockResolvedValue({
    entries: [
      {
        seq: 2,
        verb: 'RECONCILE',
        status: 'acknowledged',
        reason: null,
        issued_by: 'operator',
        queued_at_ms: 1,
        acked_at_ms: 2,
        outcome: 'ok',
        outcome_detail: 'day-3 reconciliation written',
      },
    ],
    poll_interval_ms: 1000,
  });
  issueInstanceCommand = vi.fn().mockResolvedValue({ accepted: true, command: null });
}

/** Flush microtask queue and Angular effect queue (resource loads). */
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  TestBed.flushEffects();
}

let activeFixture: { destroy(): void } | null = null;

function setup() {
  const svc = new FakeLiveRunsService();
  // The console embeds the connectivity strip and the start/stop card, which
  // inject BrokerConnectivityService. Provide a quiet fake so these tests don't
  // pull in the real BrokerHealthService / HttpClient polling chain.
  const connectivity = {
    links: () => [],
    blockers: () => [],
    daemonDown: () => false,
    fleetBlocksStarts: () => false,
    reload: () => {},
  };
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerConnectivityService, useValue: connectivity },
    ],
  });
  const fixture = TestBed.createComponent(BrokerInstancesComponent);
  activeFixture = fixture;
  fixture.detectChanges();
  return { fixture, svc, component: fixture.componentInstance };
}

afterEach(() => {
  activeFixture?.destroy();
  activeFixture = null;
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe('BrokerInstancesComponent', () => {
  it('lists every strategy instance from the fleet endpoint', async () => {
    const { fixture } = setup();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('spy_ema_paper');
    expect(text).toContain('spy_vwap_shadow');
  });

  it('shows the live binding when a running instance is selected', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.getInstanceStatus).toHaveBeenCalledWith('spy_ema_paper');
    expect(fixture.nativeElement.textContent).toContain('run-live (live)');
  });

  it('labels a dead instance as stale evidence with commands disabled', async () => {
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'idle' },
        live_binding: null,
        evidence_binding: { run_id: 'run-old', state: 'latest_run_by_ledger', is_live: false },
      }),
    );
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('stale evidence');
    expect(text).toContain('gate the next start');
  });

  it('issues durable intent and surfaces the actuation result', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    await component.setIntent('pause');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.setInstanceDesiredState).toHaveBeenCalledWith('spy_ema_paper', { action: 'pause' });
    expect(fixture.nativeElement.textContent).toContain('PAUSE queued on run-live');
  });

  it('renders the command timeline and issues one-shot commands', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    // unified entries[] timeline rendered
    expect(fixture.nativeElement.textContent).toContain('RECONCILE');
    expect(fixture.nativeElement.textContent).toContain('day-3 reconciliation written');

    await component.issueCommand('FLATTEN');
    expect(svc.issueInstanceCommand).toHaveBeenCalledWith('spy_ema_paper', { verb: 'FLATTEN' });
  });

  it('renders the engine-authored readiness verdict and gates', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Can act on the next bar?');
    expect(text).toContain('BLOCKED');
    expect(text).toContain('orders_cap');
    expect(text).toContain('4 / 4 orders used');
  });

  it('renders strategy state from spec descriptors, formatted, with no hardcoded names', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('EMA 5'); // descriptor label, not the raw column name
    expect(text).toContain('RSI');
    expect(text).toContain('624.12'); // decimal-formatted to 2 dp
    expect(text).toContain('Signal: ENTER');
  });

  it('renders the namespace-attributed broker slice', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('spy_ema_ns'); // bot_order_namespace
    expect(text).toContain('SPY'); // owned position symbol
    expect(text).toContain('1 pending order');
  });

  it('renders account contamination and the inherited banner on the instance', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    // account overview at the top
    const text1 = fixture.nativeElement.textContent ?? '';
    expect(text1).toContain('contaminated');
    expect(text1).toContain('SPY +37 unattributed');

    // inherited DEGRADED banner appears on the selected instance
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Account residual detected: DEGRADED');
  });
});
