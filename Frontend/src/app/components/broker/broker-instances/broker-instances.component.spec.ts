import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
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
    provenance: null,
    sizing: null,
    last_exit: null,
    symbol: 'SPY',
    action_plan: null,
    instrument_surface: null,
    lineage: null,
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
  emergencyFlattenAccount = vi.fn().mockResolvedValue({ accepted: true, process: { state: 'idle' } });
  getLogTail = vi.fn().mockResolvedValue([
    { ts_ms: 1_700_000_000_000, raw_text: 'bar 09:45 SPY 624.10', event_type: 'bar', consolidator_emitted: 1, snapshot_set: '{}' },
    { ts_ms: 1_700_000_001_000, raw_text: 'HALT outside_mutation: unexpected SPY +50', event_type: 'raw', consolidator_emitted: null, snapshot_set: null },
  ]);
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
    daemonFreshness: () => ({ state: 'unknown', sha: null, commitsBehind: null }),
    // Default: not paper (suppresses the Reset Paper Account row in tests
    // that don't explicitly opt in). Tests for the paper-only surface
    // override this to return true.
    isPaper: () => null as boolean | null,
    reload: () => {},
    ...connectivityOverrides,
  };
  TestBed.configureTestingModule({
    providers: [
      // PR 3 / #565: ``select(id)`` navigates to ``/broker/instances/:id``,
      // so we register the route shape the router needs to recognise that
      // URL in tests. The route does not need to actually load anything —
      // the test fixture already holds the component.
      provideRouter([
        { path: 'broker/instances', children: [] },
        { path: 'broker/instances/:id', children: [] },
      ]),
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
    // PR 3 / #565: the binding label moved off the (now removed) roster
    // button. It still renders inside the run-log target hero strip.
    expect(fixture.nativeElement.textContent).toContain('live session run-live');
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
    // PR 3 / #565: binding label moved off the roster button.
    expect(text).toContain('last session run-old');
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

  it('clears a stale optimistic actuation banner when the matching command is acknowledged (VCR-0021)', async () => {
    const { fixture, component, svc } = setup();
    // Set up an actuation with command_seq=42, and arrange the commands
    // poll to return the matching acknowledged entry.
    svc.setInstanceDesiredState.mockResolvedValue({
      durable: { state: 'STOPPED', updated_at_ms: 1, updated_by: 'operator', reason: null, version: 2 },
      actuation: {
        actuated: true,
        run_id: 'run-live',
        command_seq: 42,
        detail: 'STOP queued on run-live; awaiting ack',
      },
    });
    svc.getInstanceCommands.mockResolvedValue({
      entries: [
        {
          seq: 42,
          verb: 'STOP',
          status: 'acknowledged',
          reason: null,
          issued_by: 'operator',
          queued_at_ms: 1,
          acked_at_ms: 2,
          outcome: 'ok',
          outcome_detail: null,
        },
      ],
      poll_interval_ms: 1000,
    });

    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    await component.setIntent('stop');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    // The matching ack already landed in the polled list → banner cleared.
    expect(fixture.nativeElement.textContent).not.toContain('STOP queued on run-live; awaiting ack');
  });

  it('clears an aged optimistic actuation banner when the engine acked-on-shutdown and the commands list is empty (VCR-0021)', async () => {
    const { fixture, component, svc } = setup();
    // Engine consumes STOP via the command channel and exits cleanly before
    // writing the ack file, so the commands list goes empty. The banner
    // must age out rather than linger until the operator hard-refreshes.
    svc.setInstanceDesiredState.mockResolvedValue({
      durable: { state: 'STOPPED', updated_at_ms: 1, updated_by: 'operator', reason: null, version: 2 },
      actuation: {
        actuated: true,
        run_id: 'run-live',
        command_seq: 99,
        detail: 'STOP queued on run-live; awaiting ack',
      },
    });
    // Fresh empty array per call so each reload produces a distinct
    // resource value, forcing the downstream ``effectiveActuation``
    // computed to re-evaluate against the current wall-clock.
    svc.getInstanceCommands.mockImplementation(() =>
      Promise.resolve({ entries: [], poll_interval_ms: 1000 }),
    );

    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    // Freeze Date.now() so we can advance it past the stale threshold.
    const baseNow = 1_700_000_000_000;
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(baseNow);

    await component.setIntent('stop');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    // Before the threshold: banner still showing.
    expect(fixture.nativeElement.textContent).toContain('STOP queued on run-live; awaiting ack');

    // Advance wall-clock past the 15s stale threshold, force a re-poll so
    // ``effectiveActuation`` re-evaluates against the (still empty) list.
    nowSpy.mockReturnValue(baseNow + 16_000);
    component.commands.reload();
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('STOP queued on run-live; awaiting ack');
    nowSpy.mockRestore();
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

  it('renders the engine-authored readiness verdict and gates on Can it trade?', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    // Card label rendered as mono uppercase per the Terminal Cockpit
    // visual identity (#591) — the historic "Can it trade?" prose
    // moved into the mono "CAN IT TRADE" header.
    expect(text).toContain('CAN IT TRADE');
    expect(text).toContain('0 / 1 checks pass');
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
          halt_trigger: null,
          halt_at_ms: null,
          halt_detail: null,
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
    // #565 PR 10 — the operator-priority surface drops the static
    // "Why It Stopped" header and leads with the dynamic title (the
    // seed-day notice, in this case), so the question lands as the
    // headline rather than a generic sub-heading.
    expect(text).toContain('Needs a seed day');
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
          halt_trigger: null,
          halt_at_ms: null,
          halt_detail: null,
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

  it('names the specific safety trigger when the halt left a poison flag', async () => {
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
          hydration_accepted: null,
          hydration_failure_reason: null,
          halt_trigger: 'outside_mutation',
          halt_at_ms: 1_700_000_000_000,
          halt_detail: { symbol: 'SPY' },
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
    // The specific trigger story, not a generic "could not reconcile".
    expect(text).toContain('A trade the bot did not place');
  });

  it('flags an operator-poisoned run as unsafe even when it exited via a clean path', async () => {
    // MARK_POISONED writes poisoned.flag (halt_trigger=operator_declared) and
    // then stops through the normal shutdown path as keyboard_interrupt. The
    // run must NOT be reported as "ended cleanly".
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'idle' },
        live_binding: null,
        last_exit: {
          run_id: 'run-poison',
          ended_at_ms: 300,
          exit_code: 0,
          exit_reason: 'keyboard_interrupt',
          hydration_accepted: null,
          hydration_failure_reason: null,
          halt_trigger: 'operator_declared',
          halt_at_ms: 1_700_000_000_000,
          halt_detail: {},
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
    expect(text).not.toContain('Last session ended cleanly');
    expect(text).toContain('Run flagged unsafe');
    expect(text).toContain('An operator manually flagged this run unsafe');
  });

  it('marks a hard-failing readiness gate as FAIL · HARD', async () => {
    // makeStatus's default readiness has orders_cap failing with severity 'hard'.
    // Terminal Cockpit (#591) renders the severity as `FAIL · HARD` / `FAIL · SOFT`
    // — the historic operator-language "Blocking" / "Advisory" copy moved into
    // the visible mark.
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('FAIL · HARD');
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

  it('wires the audit-trail accordion onto the page (collapsed, header visible)', async () => {
    // PR 4 — provenance details now live behind the Audit & Diagnostics
    // accordion (`app-audit-trail-accordion`). The deep proof content is
    // covered by audit-trail-accordion.component.spec.ts. Here we only
    // assert the accordion is mounted into the parent and is collapsed by
    // default, and that the page does not regress on VCR-0014 forbidden
    // strings.
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        provenance: {
          run_id: 'abcdef0123456789',
          schema_version: '1.2',
          code_sha: 'c0ffee1234deadbeef',
          strategy_spec_path: 'spec/spy_ema_crossover.spec.json',
          strategy_spec_sha256: 'specsha',
          qc_audit_copy_path: 'references/qc-shadow/SpyEmaCrossoverAlgorithm.py',
          qc_audit_copy_sha256: 'auditsha',
          qc_cloud_backtest_id: 'd2fe45a7142e88575f6fbd75229f8681',
          account_id: 'DU1234567',
          start_date_ms: 1714838400000,
          created_at_ms: 1714838400500,
          live_config: { symbol: 'SPY' },
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
    expect(fixture.nativeElement.querySelector('app-audit-trail-accordion')).toBeTruthy();
    expect(text).toContain('Audit & Diagnostics');
    expect(text).not.toContain('Identity & Provenance');
    // The page must never surface the VCR-0014 forbidden labels — neither
    // in the parent template nor accidentally leaked from a child render.
    expect(text).not.toContain('Byte-identical to backtest');
    expect(text).not.toContain('QC-approved');
  });

  it('runs an account-wide emergency flatten after confirm + account echo', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();

    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.spyOn(window, 'prompt').mockReturnValue('du123');

    await component.issueEmergencyFlatten();

    expect(svc.emergencyFlattenAccount).toHaveBeenCalledWith('spy_ema_paper', {
      account: 'DU123',
      confirm: true,
    });
  });

  it('does not flatten when the operator cancels the confirm', async () => {
    const { fixture, component, svc } = setup();
    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();

    vi.spyOn(window, 'confirm').mockReturnValue(false);

    await component.issueEmergencyFlatten();

    expect(svc.emergencyFlattenAccount).not.toHaveBeenCalled();
  });

  it('explains a poisoned run and offers a re-deploy (fresh run_id) recovery link', async () => {
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'idle' },
        live_binding: null,
        start_defaults: {
          strategy: 'spy_ema_crossover',
          readonly: true,
          hydrate_policy: 'require',
          max_orders_per_day: 4,
          ibkr_host: '127.0.0.1',
          strategy_spec_path:
            'PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json',
          qc_audit_copy_path: 'references/qc-shadow/SpyEmaCrossoverAlgorithm.py',
          qc_cloud_backtest_id: 'd2fe45a7142e88575f6fbd75229f8681',
          account_id: 'DU1234567',
        },
        last_exit: {
          run_id: 'run-poison',
          ended_at_ms: 200,
          exit_code: 1,
          exit_reason: 'poisoned',
          hydration_accepted: null,
          hydration_failure_reason: null,
          halt_trigger: null,
          halt_at_ms: null,
          halt_detail: null,
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
    expect(text).toContain('poisoned');
    expect(text).toContain('Re-deploy (fresh run_id)');
  });

  it('builds re-deploy query params from the bound run ledger deploy identity', () => {
    const { component } = setup();
    const params = component.redeployQueryParams(
      makeStatus({
        start_defaults: {
          strategy: 'spy_ema_crossover',
          readonly: true,
          hydrate_policy: 'require',
          max_orders_per_day: 4,
          ibkr_host: '127.0.0.1',
          strategy_spec_path: 'spec/path.json',
          qc_audit_copy_path: 'audit/copy.py',
          qc_cloud_backtest_id: 'bt-hex',
          account_id: 'DU999',
        },
      }),
    );

    expect(params).toEqual({
      strategy_key: 'spy_ema_crossover',
      spec_path: 'spec/path.json',
      account_id: 'DU999',
      qc_backtest_id: 'bt-hex',
      qc_audit_copy_path: 'audit/copy.py',
      instance_id: 'spy_ema_paper',
    });
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

  function reconcileGateStatus(overrides: Partial<LiveInstanceStatus> = {}): LiveInstanceStatus {
    return makeStatus({
      readiness: {
        kind: 'live_readiness',
        as_of_ms: 1,
        source: 'engine',
        verdict: 'BLOCKED',
        summary: 'Account not reconciled today.',
        gates: [
          { name: 'latest_reconcile', status: 'fail', severity: 'hard', detail: 'No reconcile recorded today' },
        ],
      },
      ...overrides,
    });
  }

  it('renders the ADR 0008 not-wired banner whenever a live binding exists', async () => {
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(reconcileGateStatus());
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const banner = fixture.nativeElement.querySelector('[data-testid="adr-0008-not-wired-banner"]');
    expect(banner).toBeTruthy();
    expect(banner?.textContent).toContain('Runtime reconcile is not wired yet');
  });

  it('does not offer RECONCILE in the Advanced Actions list', async () => {
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(reconcileGateStatus());
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const advancedButtons = Array.from(
      fixture.nativeElement.querySelectorAll('.advanced-actions button'),
    ) as HTMLElement[];
    const labels = advancedButtons.map((b) => b.textContent ?? '');
    expect(labels.some((l) => l.includes('Re-sync'))).toBe(false);
    expect(labels.some((l) => l.includes('Close all open positions'))).toBe(true);
  });

  it('opens the run-log modal from the persistent toolbar and renders log lines', async () => {
    const { fixture, component, svc } = setup();
    // A stopped instance shows its last/evidence run, fetched once (the static
    // path), so the tail renders within the microtask flush — the live path
    // polls on a timer, which a microtask flush would not advance.
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

    const toolbarBtn = fixture.nativeElement.querySelector('.panel-toolbar .runlog-link');
    expect(toolbarBtn).toBeTruthy();
    toolbarBtn?.click();
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(svc.getLogTail).toHaveBeenCalledWith('run-old', expect.any(Number));
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Run log');
    expect(text).toContain('HALT outside_mutation');
  });

  it('closes the run-log modal on its close output', async () => {
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    component.openRunLog({ runId: 'run-live', live: true });
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.runlog-dialog')).toBeTruthy();

    component.closeRunLog();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.runlog-dialog')).toBeNull();
  });

  it('hides the Reset Paper Account button when the session is not on paper', async () => {
    // Default fake's isPaper() returns null (unknown) — the row stays hidden.
    const { fixture, component } = setup();
    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();
    component.setAdvancedOpen({
      target: Object.assign(document.createElement('details'), { open: true }),
    } as unknown as Event);
    fixture.detectChanges();
    expect((fixture.nativeElement.textContent ?? '')).not.toContain('Reset paper account');
  });

  it('keeps the roster chip consistent with the hero badge for the selected instance', async () => {
    // Repro for the prod sighting (2026-06-12 smoke run): the fleet roster
    // loaded with process_state='running' (cached daemon view), then the
    // bot exited (IBKR connection lost). The per-instance status returned
    // process.state='exited' and the hero badge correctly flipped to
    // STOPPED — but the roster chip kept saying RUNNING. The chip must
    // prefer the freshest known state for the selected row.
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        process: { state: 'exited' },
        live_binding: null,
        evidence_binding: { run_id: 'run-old', state: 'latest_run_by_ledger', is_live: false },
      }),
    );
    await flush();
    fixture.detectChanges();

    // Sanity: the tab strip's fixture row for spy_ema_paper shows RUNNING
    // (sourced from the cached fleet summary). PR 3 / #565 renamed the
    // roster to a stable-order tab strip.
    const tabStrip = fixture.nativeElement.querySelector('.tab-strip');
    expect(tabStrip?.textContent).toContain('RUNNING');

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    // After selection, the chip for spy_ema_paper must read STOPPED
    // (matching the just-loaded per-instance status), even though the
    // cached fleet summary still says 'running'.
    const selectedTab = fixture.nativeElement.querySelector('.tab-strip .tab.selected');
    expect(selectedTab?.textContent).toContain('STOPPED');
    expect(selectedTab?.textContent).not.toContain('RUNNING');
  });

  it('shows the Reset Paper Account button + how-to when the session is on paper', async () => {
    const { fixture, component } = setup({ isPaper: () => true } as never);
    await flush();
    fixture.detectChanges();
    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();
    component.setAdvancedOpen({
      target: Object.assign(document.createElement('details'), { open: true }),
    } as unknown as Event);
    fixture.detectChanges();
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Reset paper account');
    expect(text).toContain('Paper Trading Account Reset');
  });

  // PRD #593 Slice 1A (#594) — cockpit surfaces the declared action plan.
  it('renders the action-plan card when the status carries an empty action_plan', async () => {
    const { fixture, component, svc } = setup();
    svc.getInstanceStatus.mockResolvedValue(
      makeStatus({
        action_plan: { on_enter: [], on_exit: [] },
        instrument_surface: 'explicit',
      }),
    );
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    const card = fixture.nativeElement.querySelector('[data-testid="action-plan-card"]');
    expect(card).not.toBeNull();
    expect(card?.textContent ?? '').toContain(
      'Declared action plan — not active until engine consumption (Slice 4)',
    );
  });

  it('does not render the action-plan card when the ledger pre-dates the field', async () => {
    const { fixture, component } = setup();
    // The default fake returns action_plan: null (pre-Slice-1A ledger).
    await flush();
    fixture.detectChanges();

    component.select('spy_ema_paper');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(
      fixture.nativeElement.querySelector('[data-testid="action-plan-card"]'),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// PR 3 / #565 — route-driven selection.
//
// The acceptance gate requires coverage for: empty, single, N instances with
// valid id, missing id (live-bound → running → first-roster fallback), id of
// a deleted instance (graceful fallback), and multiple-bound tie behavior.
// ---------------------------------------------------------------------------

const SINGLE_FLEET: LiveInstanceSummary[] = [
  {
    strategy_instance_id: 'solo_bot',
    process_state: 'offline',
    bound_run_id: null,
    latest_run_id: null,
  },
];

const RUNNING_FALLBACK_FLEET: LiveInstanceSummary[] = [
  // No row is bound; one row is running. Default resolution should land on
  // the running row.
  {
    strategy_instance_id: 'stopped_a',
    process_state: 'offline',
    bound_run_id: null,
    latest_run_id: 'run-a',
  },
  {
    strategy_instance_id: 'running_b',
    process_state: 'running',
    bound_run_id: null,
    latest_run_id: 'run-b',
  },
];

const FIRST_ROSTER_FALLBACK_FLEET: LiveInstanceSummary[] = [
  // No row is bound and no row is running. Default resolution falls back
  // to the first row in roster order.
  {
    strategy_instance_id: 'first_in_order',
    process_state: 'offline',
    bound_run_id: null,
    latest_run_id: 'run-x',
  },
  {
    strategy_instance_id: 'second_in_order',
    process_state: 'offline',
    bound_run_id: null,
    latest_run_id: 'run-y',
  },
];

const MULTIPLE_BOUND_FLEET: LiveInstanceSummary[] = [
  // Two rows both have a bound_run_id. The first one in roster order should
  // win — the "stable deploy order" tie-break that protects muscle memory.
  {
    strategy_instance_id: 'first_bound',
    process_state: 'running',
    bound_run_id: 'run-first',
    latest_run_id: 'run-first',
  },
  {
    strategy_instance_id: 'second_bound',
    process_state: 'running',
    bound_run_id: 'run-second',
    latest_run_id: 'run-second',
  },
];

async function setupAt(routeId: string | null, fleet: LiveInstanceSummary[] = FLEET) {
  const svc = new FakeLiveRunsService();
  svc.getInstances.mockResolvedValue(fleet);
  const connectivity = {
    links: () => [],
    blockers: () => [],
    daemonDown: () => false,
    fleetBlocksStarts: () => false,
    brokerState: () => 'ok' as BrokerLinkState,
    daemonFreshness: () => ({ state: 'unknown', sha: null, commitsBehind: null }),
    isPaper: () => null as boolean | null,
    reload: () => {},
  };
  // ``provideRouter`` injects an ActivatedRoute, but without a router-outlet
  // in the fixture the route is the empty root — its paramMap never carries
  // the ``:id`` we navigate to. We stub ActivatedRoute with a paramMap
  // subject we drive manually, then keep it in sync with the Router's
  // navigation events so the canonicalisation effect can do its work.
  const paramMap$ = new BehaviorSubject(convertToParamMap({ id: routeId ?? '' }));
  const fakeRoute = { paramMap: paramMap$.asObservable() } as Partial<ActivatedRoute>;
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        { path: 'broker/instances', children: [] },
        { path: 'broker/instances/:id', children: [] },
      ]),
      { provide: LiveRunsService, useValue: svc },
      { provide: BrokerConnectivityService, useValue: connectivity },
      { provide: ActivatedRoute, useValue: fakeRoute },
    ],
  });
  const router = TestBed.inject(Router);
  // Whenever the component navigates to ``/broker/instances/:id``, mirror
  // the new id into the fake paramMap so ``selectedInstanceId`` recomputes
  // exactly as it would when bound to a live ActivatedRoute.
  router.events.subscribe((e) => {
    const evtUrl = (e as { url?: string }).url;
    if (typeof evtUrl !== 'string') return;
    const match = /\/broker\/instances\/([^/?]+)/.exec(evtUrl);
    if (match) paramMap$.next(convertToParamMap({ id: match[1] }));
  });
  if (routeId !== null) {
    await router.navigate(['/broker/instances', routeId]);
  }
  const fixture = TestBed.createComponent(BrokerInstancesComponent);
  activeFixture = fixture;
  fixture.detectChanges();
  await flush();
  fixture.detectChanges();
  return { fixture, svc, component: fixture.componentInstance };
}

describe('BrokerInstancesComponent — route-driven selection (#565 PR 3)', () => {
  it('renders an empty-state CTA when the fleet is empty', async () => {
    const { fixture, component } = await setupAt(null, []);

    expect(component.selectedInstanceId()).toBeNull();
    expect(component.showTabStrip()).toBe(false);
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('No bots deployed yet');
    // The tab strip must not render — the trader has no choice to make.
    expect(fixture.nativeElement.querySelector('.tab-strip')).toBeNull();
  });

  it('omits the tab strip when a single instance is deployed', async () => {
    const { fixture, component } = await setupAt(null, SINGLE_FLEET);

    expect(component.selectedInstanceId()).toBe('solo_bot');
    expect(component.showTabStrip()).toBe(false);
    expect(fixture.nativeElement.querySelector('.tab-strip')).toBeNull();
  });

  it('renders the tab strip when N >= 2 and respects the URL :id', async () => {
    const { fixture, component } = await setupAt('spy_vwap_shadow');

    expect(component.selectedInstanceId()).toBe('spy_vwap_shadow');
    const strip = fixture.nativeElement.querySelector('.tab-strip') as HTMLElement | null;
    expect(strip).toBeTruthy();
    if (strip === null) return;
    // Tab strip preserves the backend roster order — the same indices as
    // the LiveInstanceSummary list.
    const tabs = strip.querySelectorAll('.tab');
    expect(tabs).toHaveLength(2);
    expect(tabs[0].textContent).toContain('spy_ema_paper');
    expect(tabs[1].textContent).toContain('spy_vwap_shadow');
    // The selected tab is the one named by the URL.
    expect(strip.querySelector('.tab.selected')?.textContent).toContain('spy_vwap_shadow');
  });

  it('falls back to the first live-bound instance when the URL has no :id', async () => {
    const { component } = await setupAt(null);

    // FLEET has spy_ema_paper (bound) and spy_vwap_shadow (offline). The
    // live-bound row wins.
    expect(component.selectedInstanceId()).toBe('spy_ema_paper');
  });

  it('falls back to the first running instance when no row is bound', async () => {
    const { component } = await setupAt(null, RUNNING_FALLBACK_FLEET);

    expect(component.selectedInstanceId()).toBe('running_b');
  });

  it('falls back to the first roster row when no row is bound or running', async () => {
    const { component } = await setupAt(null, FIRST_ROSTER_FALLBACK_FLEET);

    expect(component.selectedInstanceId()).toBe('first_in_order');
  });

  it('falls back gracefully when the URL :id names a deleted instance', async () => {
    const { component } = await setupAt('long_gone_bot');

    // 'long_gone_bot' is not in FLEET. Resolver should fall back through
    // live-bound (spy_ema_paper) instead of returning null or erroring.
    expect(component.selectedInstanceId()).toBe('spy_ema_paper');
  });

  it('first-roster-order wins when multiple instances share live-bound state', async () => {
    const { component } = await setupAt(null, MULTIPLE_BOUND_FLEET);

    // Both first_bound and second_bound have bound_run_id != null. The
    // first one in roster order is the tie-break (stable deploy order).
    expect(component.selectedInstanceId()).toBe('first_bound');
  });

  it('updates the URL when the operator clicks a different tab', async () => {
    const { fixture, component } = await setupAt('spy_ema_paper');
    const router = TestBed.inject(Router);

    await component.select('spy_vwap_shadow');
    fixture.detectChanges();
    await flush();
    fixture.detectChanges();

    expect(router.url).toBe('/broker/instances/spy_vwap_shadow');
    expect(component.selectedInstanceId()).toBe('spy_vwap_shadow');
  });

  it('canonicalises the URL when a fallback resolves the selection', async () => {
    const { fixture } = await setupAt(null);
    const router = TestBed.inject(Router);
    // The canonicalisation runs through an effect that fires Router.navigate
    // fire-and-forget. Pump the microtask queue until the next event loop
    // tick so the navigation can complete before we read router.url.
    for (let i = 0; i < 5; i++) {
      await flush();
      fixture.detectChanges();
      await new Promise<void>((resolve) => queueMicrotask(resolve));
    }

    expect(router.url).toBe('/broker/instances/spy_ema_paper');
  });
});
