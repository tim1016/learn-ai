import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  BotRunView,
  RunHistoryNavigation,
  RunHistoryState,
} from '../lib/broker-v2-panel.types';
import { BotRunHistoryComponent } from './bot-run-history.component';

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
      inputs: { state: state() },
    });

    expect(screen.getByText('run-current')).toBeTruthy();
    expect(screen.getByText('Running')).toBeTruthy();
    expect(screen.getByText('process-7')).toBeTruthy();
    expect(screen.getByText('registry-2')).toBeTruthy();
    expect(screen.getByText('No terminal evidence recorded')).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'Current Run' }).getAttribute('aria-pressed'),
    ).toBe('true');
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
      inputs: { state: state() },
    });
    const requests: RunHistoryNavigation[] = [];
    fixture.componentInstance.navigationRequested.subscribe((request) =>
      requests.push(request),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));

    expect(requests).toEqual(['history']);
  });
});
