import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeBotPanelView } from '../../../../testing/bot-panel-fixtures';
import type { BotHealthCard, StartupJoinView } from '../lib/broker-v2-panel.types';
import { HealthCardComponent } from './health-card.component';

const BASE = fakeBotPanelView().health;

function preparing(overrides: Partial<StartupJoinView> = {}): StartupJoinView {
  return {
    run_id: 'run-2',
    state: 'waiting_for_stream',
    label: 'Preparing: waiting for the live stream to join',
    explanation: 'The bot has subscribed to IBKR and is waiting for its first print.',
    opened_at_ms: 1_790_000_000_000,
    live_from_ms: null,
    joined_minute_start_ms: null,
    deadline_ms: null,
    missing_start_ms: null,
    missing_end_ms: null,
    reason_code: null,
    ...overrides,
  };
}

async function renderCard(health: BotHealthCard, startupJoin: StartupJoinView | null = null) {
  return render(HealthCardComponent, { inputs: { health, startupJoin } });
}

describe('HealthCardComponent startup join (#2410)', () => {
  it('shows a run waiting for its live stream, with no deadline running yet', async () => {
    await renderCard(BASE, preparing());

    expect(screen.getByText('Preparing: waiting for the live stream to join')).toBeTruthy();
    expect(screen.queryByText('Time remaining before refusal')).toBeNull();
  });

  it('names the interval history did not return when the startup join was refused', async () => {
    await renderCard(
      BASE,
      preparing({
        state: 'refused',
        label: 'Refused: warmup history unavailable',
        live_from_ms: 1_790_000_060_000,
        deadline_ms: 1_790_000_240_000,
        missing_start_ms: 1_790_000_000_000,
        missing_end_ms: 1_790_000_060_000,
        reason_code: 'WARMUP_HISTORY_UNAVAILABLE',
      }),
    );

    expect(screen.getByText('History missing')).toBeTruthy();
    expect(screen.queryByText('Time remaining before refusal')).toBeNull();
  });

  it('says what a startup refusal left at the broker', async () => {
    await renderCard({
      ...BASE,
      running: false,
      duty_outcome: {
        kind: 'CRASHED',
        reason_code: 'RESUME_HOLE_UNFILLED',
        label: 'Refused: gap could not be filled',
        explanation: 'IBKR history did not return every regular-hours minute.',
        recorded_at_ms: 1_790_000_200_000,
        run_id: 'run-2',
        exposure_notices: [
          {
            kind: 'position_unverified',
            label: 'Position could not be verified; check the broker',
            explanation: 'The Clerk cannot currently vouch for what this bot holds.',
          },
          {
            kind: 'entry_order_working',
            label: 'An entry order is still working',
            explanation: 'If it fills, the refused bot will not manage the position it opens.',
          },
        ],
      },
    });

    const alerts = screen.getAllByRole('alert').map((alert) => alert.textContent ?? '');
    expect(alerts).toHaveLength(2);
    expect(alerts[0]).toContain('Position could not be verified; check the broker');
    expect(alerts[1]).toContain('An entry order is still working');
  });
});
