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
    last_exit: null,
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

type BrokerLinkState = 'ok' | 'down' | 'warn' | 'unknown';

function setup(connectivityOverrides: { brokerState?: () => BrokerLinkState } = {}) {
  const svc = new FakeLiveRunsService();
  // The console embeds the connectivity strip and the start/stop card, which
  // inject BrokerConnectivityService. Provide a quiet fake so these tests don't
  // pull in the real BrokerHealthService / HttpClient polling chain.
  const connectivity = {
    links: () => [],
    blockers: () => [],
    daemonDown: () => false,
    fleetBlocksStarts: () => false,
    // The broker-connection health row reads this (the real probe), not the
    // per-instance sidecar. Default to connected; tests override per case.
    brokerState: () => 'ok' as BrokerLinkState,
    reload: () => {},
    ...connectivityOverrides,
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
    expect(fixture.nativeElement.textContent).toContain('RUNNING - NOT READY');
    expect(fixture.nativeElement.textContent).toContain('Live session run-live');
  });

  it('labels a stopped instance as last-session evidence with advanced actions disabled', async () => {
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
    expect(text).toContain('STOPPED');
    expect(text).toContain('Last session run-old');
    expect(text).toContain('These take effect on the next start.');
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
    expect(text).toContain('Pre-Trade Checklist');
    expect(text).toContain('0 / 1 checks passed');
    expect(text).toContain('Daily Trade Limit Available');
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
    expect(text).toContain('spy_ema_ns'); // details still expose bot_order_namespace
    expect(text).toContain('SPY'); // owned position symbol
    expect(text).toContain('1 pending');
  });

  it('keeps the broker row CONNECTED from the live probe even when the instance has no sidecar', async () => {
    // Regression: the broker-connection row used to read `s.broker !== null`,
    // so a bot that crashed before writing its live_state sidecar showed
    // "NOT CONNECTED" while IBKR was in fact connected. The row now reads the
    // global /api/broker/health probe via connectivity.brokerState().
    const { fixture, component, svc } = setup({ brokerState: () => 'ok' });
    svc.getInstanceStatus.mockResolvedValue(makeStatus({ broker: null }));
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('CONNECTED');
    expect(text).not.toContain('NOT CONNECTED');
  });

  it('shows NOT CONNECTED when the broker probe is down, regardless of the sidecar', async () => {
    // A present sidecar must not paint the broker green when IBKR is actually
    // disconnected — the inverse of the regression above.
    const { fixture, component } = setup({ brokerState: () => 'down' });
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('NOT CONNECTED');
  });

  it('explains why a stopped instance stopped, with seed-day guidance for a cold start', async () => {
    // The console must surface *why* a run ended instead of a bare STOPPED. A
    // cold start that exits 4 with hydration failure_reason "missing" should
    // render the seed-day (Optional) remediation.
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'idle' },
        live_binding: null,
        last_exit: {
          run_id: 'run-cold',
          ended_at_ms: 200,
          exit_code: 4,
          exit_reason: 'exception',
          hydration_accepted: false,
          hydration_failure_reason: 'missing',
        },
      }),
    );
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Why It Stopped');
    expect(text).toContain('seed day');
    expect(text).toContain('Optional');
  });

  it('reports a fatal_halt as a safety halt, not a seed-day issue, even when the receipt says missing', async () => {
    // Regression: a healthy cold start (hydrate_policy=optional) leaves the
    // receipt at accepted=false/"missing", so a later fatal_halt was being
    // mis-labeled as "needs a seed day". Exit reason must win over the receipt.
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'idle' },
        live_binding: null,
        last_exit: {
          run_id: 'run-halt',
          ended_at_ms: 200,
          exit_code: 1,
          exit_reason: 'fatal_halt',
          hydration_accepted: false,
          hydration_failure_reason: 'missing',
        },
      }),
    );
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Safety halt');
    expect(text).toContain('position may still be open');
    expect(text).not.toContain('seed day');
  });

  it('does not show a "why it stopped" panel for a live instance', async () => {
    // last_exit is null while a run is live; the panel must stay hidden.
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('Why It Stopped');
  });

  it('renders account contamination and the inherited banner on the instance', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    // account overview at the top
    const text1 = fixture.nativeElement.textContent ?? '';
    expect(text1).toContain('UNRECOGNIZED POSITIONS DETECTED');
    expect(text1).toContain('SPY +37 unattributed');

    // inherited DEGRADED banner appears on the selected instance
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('UNRECOGNIZED POSITIONS DETECTED');
  });
});
