import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { BotHealthCard, BotRunView, CurrentRunState } from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE } from '../lib/broker-v2-panel.types';
import { BotBannerRunTimingComponent } from './bot-banner-run-timing.component';

const HEALTH: BotHealthCard = {
  running: true,
  phase: 'ON_DUTY',
  phase_label: 'On duty',
  desired_state: 'RUNNING',
  desired_state_label: 'Running',
  duty_outcome: null,
  last_bar_at_ms: 1_753_800_100_000,
  last_decision_at_ms: 1_753_800_200_000,
  decision_stale: false,
} as BotHealthCard;

function fakeRun(overrides: Partial<BotRunView> = {}): BotRunView {
  return {
    strategy_instance_id: 'sid-001',
    run_id: 'run-current',
    configuration_hash: 'a'.repeat(64),
    launch_reason: 'deploy',
    started_at_ms: 1_753_800_000_000,
    process: {
      strategy_instance_id: 'sid-001',
      run_id: 'run-current',
      process_identity: 'process-1',
      state: 'RUNNING',
      registry_generation: 'registry-1',
      observed_at_ms: 1_753_800_005_000,
    },
    terminal_outcome: null,
    ...overrides,
  };
}

async function renderStrip(
  runState: CurrentRunState = EMPTY_CURRENT_RUN_STATE,
  health: BotHealthCard = HEALTH,
) {
  return render(BotBannerRunTimingComponent, {
    inputs: { runState, health },
  });
}

describe('BotBannerRunTimingComponent', () => {
  it('shows Started/Ended with the last strategy bar and last decision, and a stale flag', async () => {
    await renderStrip(
      { run: fakeRun(), loading: false, failed: false },
      { ...HEALTH, decision_stale: true },
    );

    expect(screen.getByText('Started')).toBeTruthy();
    expect(screen.getByText('Running')).toBeTruthy();
    expect(
      screen.getByText(formatTimestampDisplay(HEALTH.last_bar_at_ms, { granularity: 'time' })),
    ).toBeTruthy();
    expect(
      screen.getByText(formatTimestampDisplay(HEALTH.last_decision_at_ms, { granularity: 'time' })),
    ).toBeTruthy();
    expect(screen.getByText('Stale')).toBeTruthy();
  });

  it('does not flag staleness when no decision has ever been recorded', async () => {
    await renderStrip(
      { run: fakeRun(), loading: false, failed: false },
      { ...HEALTH, last_decision_at_ms: null, decision_stale: true },
    );

    expect(screen.queryByText('Stale')).toBeNull();
  });

  it('offers a retry when the run resource failed to load, and emits it on click', async () => {
    const retried = { called: false };
    await render(BotBannerRunTimingComponent, {
      inputs: {
        runState: { run: null, loading: false, failed: true },
        health: HEALTH,
      },
      on: { retryRequested: () => { retried.called = true; } },
    });

    expect(screen.getByText(/Run timing could not be loaded\./)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry run timing' }));
    expect(retried.called).toBe(true);
  });

  it('shows no retry notice while the run resource is healthy', async () => {
    await renderStrip({ run: fakeRun(), loading: false, failed: false }, HEALTH);

    expect(screen.queryByRole('button', { name: 'Retry run timing' })).toBeNull();
  });
});
