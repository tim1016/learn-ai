import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  BotRunView,
  FeedContinuityView,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { BotRunHistoryComponent } from './bot-run-history.component';

const CONTINUITY: FeedContinuityView = {
  provider_label: 'IBKR market data',
  state: 'recovered',
  state_label: 'Recovered',
  explanation: 'IBKR market data recovered after 1 interruption in this run.',
  run_id: 'run-current',
  interruption_count: 1,
  recovery_count: 1,
  unresolved_count: 0,
  decision_impact_count: 0,
  last_interruption_at_ms: 1_753_800_000_000,
  last_recovery_at_ms: 1_753_800_022_000,
  latest_bar_at_ms: 1_753_800_010_000,
  events: [
    {
      evidence_seq: 2,
      kind: 'recovered',
      occurred_at_ms: 1_753_800_022_000,
      label: 'Feed recovered',
      explanation: 'IBKR market data resumed.',
      cause: null,
      duration_ms: 22_000,
      duration_label: '22 seconds',
      window_start_ms: null,
      window_end_ms: null,
    },
    {
      evidence_seq: 1,
      kind: 'interruption',
      occurred_at_ms: 1_753_800_000_000,
      label: 'Feed interrupted',
      explanation: 'The IBKR stream stopped delivering usable bars.',
      cause: 'stall',
      duration_ms: 22_000,
      duration_label: '22 seconds',
      window_start_ms: null,
      window_end_ms: null,
    },
  ],
};

const CURRENT_RUN: BotRunView = {
  strategy_instance_id: 'sid-001',
  run_id: 'run-current',
  configuration_hash: 'a'.repeat(64),
  launch_reason: 'deploy',
  started_at_ms: 1_753_800_000_000,
  is_current: true,
  process: {
    strategy_instance_id: 'sid-001',
    run_id: 'run-current',
    process_identity: 'process-7',
    state: 'RUNNING',
    registry_generation: 'registry-2',
    observed_at_ms: 1_753_800_005_000,
  },
  terminal_outcome: null,
};

function state(overrides: Partial<RunHistoryState> = {}): RunHistoryState {
  return {
    mode: 'current',
    current: CURRENT_RUN,
    history: null,
    currentLoading: false,
    historyLoading: false,
    currentFailed: false,
    historyFailed: false,
    canViewNewer: false,
    ...overrides,
  };
}

describe('BotRunHistoryComponent', () => {
  it('shows the backend-owned current process evidence without inferring terminal state', async () => {
    await render(BotRunHistoryComponent, {
      inputs: {
        state: state(),
        botRunning: true,
        feedContinuity: CONTINUITY,
      },
    });

    expect(screen.getByText('run-current')).toBeTruthy();
    expect(screen.getByText('Running')).toBeTruthy();
    expect(screen.getByText('process-7')).toBeTruthy();
    expect(screen.getByText('registry-2')).toBeTruthy();
    expect(screen.getByText('No terminal evidence recorded')).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'Current Run' }).getAttribute('aria-pressed'),
    ).toBe('true');
    expect(screen.getByText('Evidence is updating')).toBeTruthy();
  });

  it('keeps cached idle evidence visible during a background refresh', async () => {
    await render(BotRunHistoryComponent, {
      inputs: {
        state: state({ currentLoading: true }),
        botRunning: false,
        feedContinuity: CONTINUITY,
      },
    });

    expect(screen.getByText('run-current')).toBeTruthy();
    expect(screen.getByText('No changes since')).toBeTruthy();
    expect(screen.queryByText('Loading run evidence…')).toBeNull();
  });

  it('labels historical evidence and makes the live-control target explicit', async () => {
    const previousRun: BotRunView = {
      ...CURRENT_RUN,
      run_id: 'run-previous',
      launch_reason: 'resume',
      is_current: false,
      process: null,
      terminal_outcome: {
        kind: 'CLOCKED_OUT_FLAT',
        reason_code: 'SESSION_COMPLETE',
        recorded_at_ms: 1_753_900_000_000,
        run_id: 'run-previous',
      },
    };

    await render(BotRunHistoryComponent, {
      inputs: {
        state: state({
          mode: 'history',
          history: { runs: [previousRun], next_cursor: null },
        }),
        feedContinuity: CONTINUITY,
      },
    });

    expect(screen.getByText('run-previous')).toBeTruthy();
    expect(screen.getByText('Clocked Out Flat')).toBeTruthy();
    expect(
      screen.getByText('Viewing history — controls apply to the current run.'),
    ).toBeTruthy();
  });

  it('emits navigation requests without owning command state', async () => {
    const { fixture } = await render(BotRunHistoryComponent, {
      inputs: { state: state(), feedContinuity: CONTINUITY },
    });
    const requests: RunHistoryNavigation[] = [];
    fixture.componentInstance.navigationRequested.subscribe((request) =>
      requests.push(request),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));

    expect(requests).toEqual(['history']);
  });

  it('keeps newer-page navigation available when the current history request fails', async () => {
    const { fixture } = await render(BotRunHistoryComponent, {
      inputs: {
        state: state({
          mode: 'history',
          historyFailed: true,
          canViewNewer: true,
        }),
        feedContinuity: CONTINUITY,
      },
    });
    const requests: RunHistoryNavigation[] = [];
    fixture.componentInstance.navigationRequested.subscribe((request) =>
      requests.push(request),
    );

    const newerRun = screen.getByRole('button', { name: 'Newer run' });
    expect(newerRun.hasAttribute('disabled')).toBe(false);
    fireEvent.click(newerRun);

    expect(requests).toEqual(['newer']);
  });

  it('shows current-run IBKR interruption and recovery evidence', async () => {
    await render(BotRunHistoryComponent, {
      inputs: { state: state(), feedContinuity: CONTINUITY },
    });

    expect(screen.getByText('IBKR market-data continuity')).toBeTruthy();
    expect(screen.getByText('Feed interrupted')).toBeTruthy();
    expect(screen.getByText('Feed recovered')).toBeTruthy();
    expect(screen.getAllByText('Duration 22 seconds')).toHaveLength(2);
  });

  it('does not turn unavailable continuity evidence into zero incidents', async () => {
    await render(BotRunHistoryComponent, {
      inputs: {
        state: state(),
        feedContinuity: {
          ...CONTINUITY,
          state: 'not_recorded',
          state_label: 'Continuity not recorded',
          explanation: 'Run-scoped continuity evidence is unavailable.',
          interruption_count: 0,
          recovery_count: 0,
          decision_impact_count: 0,
          events: [],
        },
      },
    });

    expect(screen.getAllByText('—')).toHaveLength(3);
    expect(screen.getByText('No run-scoped continuity evidence is available yet.')).toBeTruthy();
    expect(screen.queryByText('No feed interruptions have been recorded in this run.')).toBeNull();
  });
});
