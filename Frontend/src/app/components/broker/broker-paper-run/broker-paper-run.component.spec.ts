import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi, afterEach } from 'vitest';
import { LiveRunsService } from '../../../services/live-runs.service';
import { BrokerPaperRunComponent } from './broker-paper-run.component';
import type {
  ArtifactsSummary,
  DecisionsSummary,
  ExecutionsSummary,
  FlagsSummary,
  HostRunnerHealth,
  LiveRunStatus,
  LiveRunSummary,
  ReconcileSummary,
  RunState,
  TradesSummary,
} from '../../../api/live-runs.types';

class FakeLiveRunsService {
  listRuns = vi.fn().mockResolvedValue([]);
  getStatus = vi.fn().mockResolvedValue(null);
  getLogTail = vi.fn().mockResolvedValue([]);
  getHostRunnerHealth = vi.fn().mockResolvedValue(makeHostRunnerHealth());
  startHostRunner = vi.fn().mockResolvedValue({
    accepted: true,
    process: makeHostRunnerHealth({ state: 'running', run_id: RUN_ID }).process,
  });
  stopHostRunner = vi.fn().mockResolvedValue({
    accepted: true,
    process: makeHostRunnerHealth({ state: 'exited', run_id: RUN_ID, exit_code: 0 }).process,
  });
}

const RUN_ID = 'aaaaaaaa-0000-0000-0000-000000000001';

function makeRun(overrides: Partial<LiveRunSummary> = {}): LiveRunSummary {
  return {
    run_id: RUN_ID,
    account_id: 'DU1234567',
    session_start_ms: 1_700_000_000_000,
    created_at_ms: 1_700_000_000_000,
    run_started_at_ms: 1_700_000_000_000,
    ended_at_ms: null,
    last_activity_ms: 1_700_000_000_000,
    state: 'running',
    decision_count: 5,
    execution_count: 2,
    halt_flag_set: false,
    poisoned_flag_set: false,
    ...overrides,
  };
}

const EMPTY_DECISIONS: DecisionsSummary = { row_count: 0, latest_decision: null };
const EMPTY_EXECUTIONS: ExecutionsSummary = { row_count: 0, last_fills: [] };
const EMPTY_TRADES: TradesSummary = { row_count: 0, open_position: null };
const EMPTY_FLAGS: FlagsSummary = { halt_flag: null, poisoned_flag: null };
const EMPTY_ARTIFACTS: ArtifactsSummary = { files: [] };
const EMPTY_RECONCILE: ReconcileSummary = { latest_receipt_name: null, latest_receipt_url: null };

function makeStatus(state: RunState, overrides: Partial<LiveRunStatus> = {}): LiveRunStatus {
  return {
    run_id: RUN_ID,
    account_id: 'DU1234567',
    state,
    last_bar_time_ms: 1_700_000_000_000,
    last_bar_age_s: 30,
    heartbeat_parse_status: 'ok',
    decisions: EMPTY_DECISIONS,
    executions: EMPTY_EXECUTIONS,
    trades: EMPTY_TRADES,
    flags: EMPTY_FLAGS,
    artifacts: EMPTY_ARTIFACTS,
    reconcile: EMPTY_RECONCILE,
    fetched_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

function makeHostRunnerHealth(
  processOverrides: Partial<HostRunnerHealth['process']> = {},
): HostRunnerHealth {
  return {
    ok: true,
    repo_root: 'C:/repo',
    live_runs_root: 'C:/repo/PythonDataService/artifacts/live_runs',
    fetched_at_ms: 1_700_000_000_000,
    process: {
      state: 'idle',
      run_id: null,
      pid: null,
      started_at_ms: null,
      ended_at_ms: null,
      exit_code: null,
      command: [],
      log_path: null,
      message: 'No host runner process.',
      ...processOverrides,
    },
  };
}

/** Flush microtask queue and Angular effect queue. */
async function flush() {
  await Promise.resolve();
  await Promise.resolve();
  TestBed.flushEffects();
}

function setup(runs: LiveRunSummary[] = [], status: LiveRunStatus | null = null) {
  const svc = new FakeLiveRunsService();
  svc.listRuns.mockResolvedValue(runs);
  if (status !== null) svc.getStatus.mockResolvedValue(status);

  TestBed.configureTestingModule({
    providers: [{ provide: LiveRunsService, useValue: svc }],
  });
  const fixture = TestBed.createComponent(BrokerPaperRunComponent);
  fixture.detectChanges();
  return { fixture, svc, component: fixture.componentInstance };
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

// ── Filter chip logic ─────────────────────────────────────────────────

describe('BrokerPaperRunComponent — filteredRuns', () => {
  it('all filter returns every run', async () => {
    const runs = [
      makeRun({ run_id: 'aaa', state: 'running' }),
      makeRun({ run_id: 'bbb', state: 'halted' }),
      makeRun({ run_id: 'ccc', state: 'complete' }),
    ];
    const { component } = setup(runs);
    await flush();
    component.setFilter('all');
    expect(component.filteredRuns()).toHaveLength(3);
  });

  it('halted filter returns only halted runs', async () => {
    const runs = [
      makeRun({ run_id: 'aaa', state: 'running' }),
      makeRun({ run_id: 'bbb', state: 'halted' }),
      makeRun({ run_id: 'ccc', state: 'halted' }),
    ];
    const { component } = setup(runs);
    await flush();
    component.setFilter('halted');
    const filtered = component.filteredRuns();
    expect(filtered).toHaveLength(2);
    expect(filtered.every((r) => r.state === 'halted')).toBe(true);
  });

  it('complete filter returns only complete runs', async () => {
    const runs = [
      makeRun({ run_id: 'aaa', state: 'running' }),
      makeRun({ run_id: 'bbb', state: 'complete' }),
    ];
    const { component } = setup(runs);
    await flush();
    component.setFilter('complete');
    const filtered = component.filteredRuns();
    expect(filtered).toHaveLength(1);
    expect(filtered[0].state).toBe('complete');
  });
});

// ── Auto-select ───────────────────────────────────────────────────────

describe('BrokerPaperRunComponent — auto-select', () => {
  it('selects the first active run (running) when the list loads', async () => {
    const runs = [
      makeRun({ run_id: 'idle-run', state: 'idle' }),
      makeRun({ run_id: 'running-run', state: 'running' }),
    ];
    const { component } = setup(runs, makeStatus('running'));
    await flush();
    expect(component.selectedRunId()).toBe('running-run');
  });

  it('falls back to the first run if no active run exists', async () => {
    const runs = [
      makeRun({ run_id: 'first-run', state: 'complete' }),
      makeRun({ run_id: 'second-run', state: 'idle' }),
    ];
    const { component } = setup(runs, makeStatus('complete'));
    await flush();
    expect(component.selectedRunId()).toBe('first-run');
  });

  it('does not override an already-set selectedRunId', async () => {
    const runs = [makeRun({ run_id: 'list-run', state: 'running' })];
    const { component } = setup(runs, makeStatus('running'));
    component.selectRun('pre-selected-run');
    await flush();
    expect(component.selectedRunId()).toBe('pre-selected-run');
  });
});

// ── actionRequired — all 10 RunState values ───────────────────────────

describe('BrokerPaperRunComponent — actionRequired for each state', () => {
  const actionStates: RunState[] = ['halted', 'poisoned', 'stopped'];
  const passiveStates: RunState[] = [
    'idle',
    'waiting_for_bars',
    'warming_up',
    'running',
    'stale',
    'complete',
    'unknown',
  ];

  for (const state of actionStates) {
    it(`actionRequired is true when state is ${state}`, async () => {
      const runs = [makeRun({ run_id: RUN_ID, state })];
      const status = makeStatus(state);
      const { component } = setup(runs, status);
      await flush();
      // Let status resource load after auto-select fires
      await flush();
      expect(component.actionRequired()).toBe(true);
    });
  }

  for (const state of passiveStates) {
    it(`actionRequired is false when state is ${state}`, async () => {
      const runs = [makeRun({ run_id: RUN_ID, state })];
      const status = makeStatus(state);
      const { component } = setup(runs, status);
      await flush();
      await flush();
      expect(component.actionRequired()).toBe(false);
    });
  }

  it('actionRequired is false when no run is selected (status is null)', () => {
    const { component } = setup();
    expect(component.actionRequired()).toBe(false);
  });
});

// ── topStripDynamicClasses ─────────────────────────────────────────────

describe('BrokerPaperRunComponent — topStripDynamicClasses', () => {
  it('includes state-running class for running state', async () => {
    const runs = [makeRun({ run_id: RUN_ID, state: 'running' })];
    const { component } = setup(runs, makeStatus('running'));
    await flush();
    await flush();
    expect(component.topStripDynamicClasses()).toContain('state-running');
  });

  it('includes action-required for halted state', async () => {
    const runs = [makeRun({ run_id: RUN_ID, state: 'halted' })];
    const { component } = setup(runs, makeStatus('halted'));
    await flush();
    await flush();
    const classes = component.topStripDynamicClasses();
    expect(classes).toContain('state-halted');
    expect(classes).toContain('action-required');
  });

  it('does not include action-required for idle state', async () => {
    const runs = [makeRun({ run_id: RUN_ID, state: 'idle' })];
    const { component } = setup(runs, makeStatus('idle'));
    await flush();
    await flush();
    expect(component.topStripDynamicClasses()).not.toContain('action-required');
  });

  it('returns empty string when no run is selected', () => {
    const { component } = setup();
    expect(component.topStripDynamicClasses()).toBe('');
  });
});

// ── warmupProgress ────────────────────────────────────────────────────

describe('BrokerPaperRunComponent — warmupProgress', () => {
  it('returns — when no status', () => {
    const { component } = setup();
    expect(component.warmupProgress()).toBe('—');
  });

  it('formats row_count / 15', async () => {
    const runs = [makeRun({ run_id: RUN_ID, state: 'warming_up' })];
    const status = makeStatus('warming_up', {
      decisions: { row_count: 7, latest_decision: null },
    });
    const { component } = setup(runs, status);
    await flush();
    await flush();
    expect(component.warmupProgress()).toBe('7 / 15');
  });
});

// ── lastBarAgeLabel ───────────────────────────────────────────────────

describe('BrokerPaperRunComponent — lastBarAgeLabel', () => {
  it('returns — when no status', () => {
    const { component } = setup();
    expect(component.lastBarAgeLabel()).toBe('—');
  });

  it('returns — when last_bar_age_s is null', async () => {
    const runs = [makeRun({ run_id: RUN_ID })];
    const status = makeStatus('running', { last_bar_age_s: null });
    const { component } = setup(runs, status);
    await flush();
    await flush();
    expect(component.lastBarAgeLabel()).toBe('—');
  });

  it('formats seconds when age < 60', async () => {
    const runs = [makeRun({ run_id: RUN_ID })];
    const status = makeStatus('running', { last_bar_age_s: 45 });
    const { component } = setup(runs, status);
    await flush();
    await flush();
    expect(component.lastBarAgeLabel()).toBe('45 s ago');
  });

  it('formats minutes when age >= 60', async () => {
    const runs = [makeRun({ run_id: RUN_ID })];
    const status = makeStatus('running', { last_bar_age_s: 125 });
    const { component } = setup(runs, status);
    await flush();
    await flush();
    expect(component.lastBarAgeLabel()).toBe('2 m ago');
  });
});

// ── lastBarAgeStale ───────────────────────────────────────────────────

describe('BrokerPaperRunComponent — lastBarAgeStale', () => {
  it('is false when no status', () => {
    const { component } = setup();
    expect(component.lastBarAgeStale()).toBe(false);
  });

  it('is false when age is exactly 90 s', async () => {
    const runs = [makeRun({ run_id: RUN_ID })];
    const status = makeStatus('running', { last_bar_age_s: 90 });
    const { component } = setup(runs, status);
    await flush();
    await flush();
    expect(component.lastBarAgeStale()).toBe(false);
  });

  it('is true when age exceeds 90 s', async () => {
    const runs = [makeRun({ run_id: RUN_ID })];
    const status = makeStatus('stale', { last_bar_age_s: 91 });
    const { component } = setup(runs, status);
    await flush();
    await flush();
    expect(component.lastBarAgeStale()).toBe(true);
  });
});

// ── runIdShort ────────────────────────────────────────────────────────

describe('BrokerPaperRunComponent — runIdShort', () => {
  it('returns — when no run is selected', () => {
    const { component } = setup();
    expect(component.runIdShort()).toBe('—');
  });

  it('returns the first 8 characters of the run ID', () => {
    const { component } = setup();
    component.selectRun('abcdef12-1234-0000-0000-000000000000');
    expect(component.runIdShort()).toBe('abcdef12');
  });
});

// ── host runner controls ──────────────────────────────────────────────

describe('BrokerPaperRunComponent — host runner controls', () => {
  it('starts the selected run through the host daemon with hydrate policy', async () => {
    const runs = [makeRun({ run_id: RUN_ID, state: 'idle' })];
    const { component, svc } = setup(runs, makeStatus('idle'));
    await flush();
    await flush();

    component.setRunnerHydratePolicy('optional');
    await component.startHostRunner();

    expect(svc.startHostRunner).toHaveBeenCalledWith(RUN_ID, {
      readonly: true,
      hydrate_policy: 'optional',
      strategy: 'spy_ema_crossover',
      max_orders_per_day: 4,
      ibkr_host: '127.0.0.1',
    });
    expect(component.runnerError()).toBeNull();
  });

  it('stops the selected run through the host daemon', async () => {
    const runs = [makeRun({ run_id: RUN_ID, state: 'running' })];
    const { component, svc } = setup(runs, makeStatus('running'));
    await flush();
    await flush();

    await component.stopHostRunner();

    expect(svc.stopHostRunner).toHaveBeenCalledWith(RUN_ID, { force: false });
    expect(component.runnerError()).toBeNull();
  });
});
