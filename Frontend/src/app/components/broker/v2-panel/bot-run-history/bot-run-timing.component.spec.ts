import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { BotHealthCard, BotRunView } from '../lib/broker-v2-panel.types';
import { BotRunTimingComponent } from './bot-run-timing.component';

const RUN: BotRunView = {
  strategy_instance_id: 'live-ema-spy-0917',
  run_id: 'run-current',
  configuration_hash: 'a'.repeat(64),
  launch_reason: 'deploy',
  started_at_ms: 1789653688134,
  is_current: true,
  process: null,
  terminal_outcome: {
    kind: 'STOPPED', reason_code: 'STOPPED_FLAT',
    recorded_at_ms: 1789677191285, run_id: 'run-current',
  },
};

const HEALTH: BotHealthCard = {
  strategy_instance_id: RUN.strategy_instance_id,
  phase: 'OFF_DUTY', phase_label: 'Off duty',
  desired_state: 'STOPPED', desired_state_label: 'Stopped', running: false,
  duty_outcome: null, last_bar_at_ms: 1789675200000,
  last_decision_at_ms: 1789675205000, decision_stale: false,
  resume_eligible: true, resume_label: 'Resume', resume_explanation: '',
  carryover_checkpoint_exposure: {},
};

describe('BotRunTimingComponent', () => {
  it('shows the latest run start and recorded end in Trader without audit identifiers', async () => {
    await render(BotRunTimingComponent, {
      inputs: { state: { run: RUN, loading: false, failed: false }, health: HEALTH },
    });
    const timing = within(screen.getByRole('region', { name: 'Latest run timing' }));
    expect(timing.getByText('Started')).toBeTruthy();
    expect(timing.getByText(formatTimestampDisplay(RUN.started_at_ms))).toBeTruthy();
    expect(timing.getByText(formatTimestampDisplay(1789677191285))).toBeTruthy();
    expect(timing.queryByText('Last strategy bar')).toBeNull();
    expect(timing.queryByText(RUN.run_id)).toBeNull();
  });

  it('adds the distinct evaluated-bar and decision-receipt clocks for Operator', async () => {
    await render(BotRunTimingComponent, {
      inputs: { state: { run: RUN, loading: false, failed: false },
        health: { ...HEALTH, decision_stale: true }, operator: true },
    });
    expect(screen.getByText('Last strategy bar')).toBeTruthy();
    expect(screen.getByText(formatTimestampDisplay(HEALTH.last_bar_at_ms))).toBeTruthy();
    expect(screen.getByText(formatTimestampDisplay(HEALTH.last_decision_at_ms))).toBeTruthy();
    expect(screen.getByText('Stale')).toBeTruthy();
  });

  it('does not invent an end time from an off-duty snapshot or another run’s outcome', async () => {
    await render(BotRunTimingComponent, {
      inputs: { state: { run: { ...RUN, terminal_outcome: null }, loading: false, failed: false },
        health: { ...HEALTH, duty_outcome: { kind: 'STOPPED', reason_code: 'STOPPED_FLAT',
          label: 'Stopped', explanation: '', recorded_at_ms: 1789677191285, run_id: 'older-run' } } },
    });
    expect(screen.getByText('No end recorded')).toBeTruthy();
    expect(screen.queryByText(formatTimestampDisplay(1789677191285))).toBeNull();
  });

  it('shows Still running only when the run has live process evidence', async () => {
    await render(BotRunTimingComponent, {
      inputs: { state: { run: { ...RUN, terminal_outcome: null, process: {
        strategy_instance_id: RUN.strategy_instance_id, run_id: RUN.run_id,
        state: 'RUNNING', process_identity: 'process-1', registry_generation: 'registry-1',
        observed_at_ms: 1789653689000,
      } }, loading: false, failed: false }, health: HEALTH },
    });
    expect(screen.getByText('Still running')).toBeTruthy();
  });

  it('keeps missing clocks explicit and offers a retry when run evidence fails', async () => {
    const { fixture } = await render(BotRunTimingComponent, {
      inputs: { state: { run: null, loading: false, failed: true },
        health: { ...HEALTH, last_bar_at_ms: null, last_decision_at_ms: null }, operator: true },
    });
    let retried = false;
    fixture.componentInstance.retryRequested.subscribe(() => { retried = true; });
    expect(screen.getAllByText('Unavailable')).toHaveLength(2);
    expect(screen.getAllByText('None recorded')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Retry run timing' }));
    expect(retried).toBe(true);
  });
});
