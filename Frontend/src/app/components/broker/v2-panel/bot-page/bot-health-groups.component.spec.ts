/** #2794 R9, R5: health in groups, and the run's own facts the old health card showed. */
import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeBotPage, fakeBotPanelView } from '../../../../testing/bot-panel-fixtures';
import type { BotHealthCard, BotPanelView, StartupJoinView } from '../lib/broker-v2-panel.types';
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

const BLOCKED: BotPanelView['mission_verdict'] = {
  state: 'blocked',
  label: 'Mission blocked',
  explanation: 'The exit did not flatten the position.',
  next_action: 'Check against Alpaca, then sell what is left.',
  evaluated_at_ms: 1_790_000_000_000,
  recovery_status: {
    kind: 'allowed_from', reason_code: 'NO_SESSION_OPEN', explanation: 'No session is open.',
    allowed_from_ms: 1_790_003_600_000,
  },
};

describe('BotHealthGroupsComponent (#2794)', () => {
  it('shows one market read when listened and traded underlying are the same', async () => {
    const pulse = fakeBotPanelView().market_pulse;
    await render(BotHealthGroupsComponent, { inputs: {
      health: HEALTH, marketPulse: pulse, tradedMarketPulse: pulse, listensTo: 'SPY', trades: 'SPY',
    } });
    expect(screen.getAllByRole('heading', { name: 'Market right now' })).toHaveLength(1);
  });
  it('renders each backend health line with its state, value and explanation', async () => {
    await render(BotHealthGroupsComponent, {
      inputs: { health: {
        run: [{ key: 'orders', label: 'Orders sent', state: 'attention', value: 'Not recorded', note: 'Run evidence is unreadable.', at_ms: null }],
        account: [{ key: 'holds', label: 'Holds', state: 'ok', value: 'None', note: null, at_ms: null }],
      } },
    });

    const run = screen.getByRole('list', { name: 'During this run' });
    const line = within(run).getByText('Orders sent').closest('li');
    expect(line?.getAttribute('data-state')).toBe('attention');
    expect(line?.textContent).toContain('Not recorded');
    expect(line?.textContent).toContain('Run evidence is unreadable.');
    expect(within(screen.getByRole('list', { name: 'Account right now' })).getByText('Holds')).toBeTruthy();
  });

  it('reads the market for both symbols when the bot trades something other than what it listens to (#2912)', async () => {
    const pulse = fakeBotPanelView().market_pulse;
    await render(BotHealthGroupsComponent, {
      inputs: {
        health: HEALTH,
        marketPulse: pulse,
        tradedMarketPulse: { ...pulse, headline: 'QQQ is halted', explanation: 'Trading in QQQ is halted.', attention_required: true },
        listensTo: 'SPY',
        trades: 'QQQ',
      },
    });

    const listened = screen.getByRole('heading', { name: 'Market right now · listens to SPY' });
    const traded = screen.getByRole('heading', { name: 'Market right now · trades QQQ' });
    expect(listened).toBeTruthy();
    expect(traded.nextElementSibling?.textContent).toContain('QQQ is halted');
  });

  it('heads the one market read plainly for a bot that trades what it listens to', async () => {
    await render(BotHealthGroupsComponent, {
      inputs: { health: HEALTH, marketPulse: fakeBotPanelView().market_pulse, listensTo: 'SPY' },
    });

    expect(screen.getByRole('heading', { name: 'Market right now' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: /trades/ })).toBeNull();
  });

  it('says how a run is joining its live stream, under the run', async () => {
    await render(BotHealthGroupsComponent, { inputs: { health: HEALTH, startupJoin: JOINING } });

    expect(screen.getByText('Preparing: waiting for the live stream to join')).toBeTruthy();
  });

  it('gives the Clerk’s explanation, next step and next retry under the account, never its "Mission blocked" label', async () => {
    const { container } = await render(BotHealthGroupsComponent, { inputs: { health: HEALTH, clerkGuidance: BLOCKED } });

    const guidance = container.querySelector('.health__guidance') as HTMLElement;
    expect(guidance.textContent).toContain('The exit did not flatten the position.');
    expect(guidance.textContent).toContain('Check against Alpaca, then sell what is left.');
    expect(guidance.textContent).toContain('Automatic retry: allowed from');
    expect(screen.queryByText('Mission blocked')).toBeNull();
  });

  it('says how a run ended and what it left at the broker', async () => {
    const { container } = await render(BotHealthGroupsComponent, { inputs: { health: HEALTH, dutyOutcome: REFUSED } });

    const outcome = container.querySelector('.health__outcome') as HTMLElement;
    expect(within(outcome).getByText('Refused: gap could not be filled')).toBeTruthy();
    expect(within(outcome).getByText('An entry order is still working')).toBeTruthy();
  });
});
