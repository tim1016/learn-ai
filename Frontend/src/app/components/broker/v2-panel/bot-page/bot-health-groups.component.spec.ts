/** #2794 R9, R5: health in groups, and the run's own facts the old health card showed. */
import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeBotPage, fakeBotPanelView } from '../../../../testing/bot-panel-fixtures';
import type { BotHealthCard, StartupJoinView } from '../lib/broker-v2-panel.types';
import { BotHealthGroupsComponent } from './bot-health-groups.component';

const HEALTH = fakeBotPage(fakeBotPanelView()).health;

const JOINING: StartupJoinView = {
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
};

const REFUSED: NonNullable<BotHealthCard['duty_outcome']> = {
  kind: 'CRASHED',
  reason_code: 'RESUME_HOLE_UNFILLED',
  label: 'Refused: gap could not be filled',
  explanation: 'IBKR history did not return every regular-hours minute.',
  recorded_at_ms: 1_790_000_200_000,
  run_id: 'run-2',
  exposure_notices: [{
    kind: 'entry_order_working',
    label: 'An entry order is still working',
    explanation: 'If it fills, the refused bot will not manage the position it opens.',
  }],
};

describe('BotHealthGroupsComponent (#2794)', () => {
  it('splits health into this run and the account right now', async () => {
    await render(BotHealthGroupsComponent, { inputs: { health: HEALTH } });

    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent?.trim()))
      .toEqual(['During this run', 'Account right now']);
  });

  it('says how a run is joining its live stream, under the run', async () => {
    await render(BotHealthGroupsComponent, { inputs: { health: HEALTH, startupJoin: JOINING } });

    expect(screen.getByText('Preparing: waiting for the live stream to join')).toBeTruthy();
  });

  it('says how a run ended and what it left at the broker', async () => {
    const { container } = await render(BotHealthGroupsComponent, { inputs: { health: HEALTH, dutyOutcome: REFUSED } });

    const outcome = container.querySelector('.health__outcome') as HTMLElement;
    expect(within(outcome).getByText('Refused: gap could not be filled')).toBeTruthy();
    expect(within(outcome).getByText('An entry order is still working')).toBeTruthy();
  });
});
