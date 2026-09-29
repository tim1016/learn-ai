import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { AccountWorkspaceLink } from '../../../../fleet/account-workspace';
import type { BotRunView, CurrentRunState, FeedContinuityView } from '../lib/broker-v2-panel.types';
import { BotRunHistoryComponent } from './bot-run-history.component';

const HISTORY: AccountWorkspaceLink = {
  commands: ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'history'],
  queryParams: { account: 'clrk_spec' },
};

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

function state(overrides: Partial<CurrentRunState> = {}): CurrentRunState {
  return { run: CURRENT_RUN, loading: false, failed: false, ...overrides };
}

function renderRuns(inputs: Record<string, unknown>) {
  return render(BotRunHistoryComponent, {
    inputs: { historyLink: HISTORY, ...inputs },
    providers: [provideRouter([])],
  });
}

describe('BotRunHistoryComponent', () => {
  it('shows the backend-owned current process evidence without inferring terminal state', async () => {
    await renderRuns({
      state: state(),
      botRunning: true,
      feedContinuity: CONTINUITY,
    });

    expect(screen.getByText('run-current')).toBeTruthy();
    expect(screen.getByText('Running')).toBeTruthy();
    expect(screen.getByText('process-7')).toBeTruthy();
    expect(screen.getByText('registry-2')).toBeTruthy();
    expect(screen.getByText('No terminal evidence recorded')).toBeTruthy();
    expect(screen.getByText('Evidence is updating')).toBeTruthy();
  });

  it('keeps cached idle evidence visible during a background refresh', async () => {
    await renderRuns({
      state: state({ loading: true }),
      botRunning: false,
      feedContinuity: CONTINUITY,
    });

    expect(screen.getByText('run-current')).toBeTruthy();
    expect(screen.getByText('No changes since')).toBeTruthy();
    expect(screen.queryByText('Loading run evidence…')).toBeNull();
  });

  it('sends earlier runs to History instead of paging through them one at a time', async () => {
    await renderRuns({ state: state(), feedContinuity: CONTINUITY });

    expect(screen.getByRole('link', { name: 'History' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/history?account=clrk_spec');
    expect(screen.queryByRole('button', { name: 'Previous Runs' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Older run' })).toBeNull();
  });

  it('offers a retry when the current run could not be read', async () => {
    const { fixture } = await renderRuns({ state: state({ run: null, failed: true }), feedContinuity: CONTINUITY });
    let retried = 0;
    fixture.componentInstance.retryRequested.subscribe(() => retried++);

    expect(screen.getByRole('alert').textContent).toContain('Run evidence is unavailable.');
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(retried).toBe(1);
  });

  it('shows current-run IBKR interruption and recovery evidence', async () => {
    await renderRuns({ state: state(), feedContinuity: CONTINUITY });

    expect(screen.getByText('IBKR market-data continuity')).toBeTruthy();
    expect(screen.getByText('Feed interrupted')).toBeTruthy();
    expect(screen.getByText('Feed recovered')).toBeTruthy();
    expect(screen.getAllByText('Duration 22 seconds')).toHaveLength(2);
  });

  it('does not turn unavailable continuity evidence into zero incidents', async () => {
    await renderRuns({
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
    });

    expect(screen.getAllByText('—')).toHaveLength(3);
    expect(screen.getByText('No run-scoped continuity evidence is available yet.')).toBeTruthy();
    expect(screen.queryByText('No feed interruptions have been recorded in this run.')).toBeNull();
  });
});
