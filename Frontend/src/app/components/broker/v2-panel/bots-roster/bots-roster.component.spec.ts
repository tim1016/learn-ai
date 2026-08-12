import { render, screen, fireEvent } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import type { BotCatalogView } from '../lib/broker-v2-panel.types';
import { BotsRosterComponent } from './bots-roster.component';

function fakeBot(overrides: Partial<BotCatalogView> = {}): BotCatalogView {
  return {
    strategy_instance_id: 'spy-01',
    broker: 'alpaca',
    account_id: 'PA9',
    symbol: 'SPY',
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    running: true,
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    mode: 'trade',
    status_label: 'Working',
    status_explanation: 'Running under Account Clerk custody.',
    exposure: {},
    fills_today: 1,
    realized_pnl_today: 10,
    open_pnl: null,
    last_activity_at_ms: 1_700_000_000_000,
    needs_attention: false,
    ...overrides,
  };
}

function fakeRowAction(
  actionId: 'resume' | 'stop' = 'stop',
): NonNullable<BotCatalogView['row_action']> {
  return {
    action_id: actionId,
    label: actionId === 'resume' ? 'Resume' : 'Stop',
    explanation: `${actionId} this bot.`,
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
  };
}

async function renderRoster(
  bots: BotCatalogView[],
  pendingBotIds: ReadonlySet<string> = new Set(),
) {
  return render(BotsRosterComponent, {
    providers: [provideRouter([])],
    componentInputs: {
      bots,
      broker: 'alpaca',
      pendingBotIds,
    },
  });
}

describe('BotsRosterComponent', () => {
  it('renders all bot rows', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'spy-01', symbol: 'SPY', strategy_label: 'Opening Range Breakout' }),
      fakeBot({ strategy_instance_id: 'qqq-01', symbol: 'QQQ' }),
    ];
    const { container } = await renderRoster(bots);

    expect(await screen.findByText('spy-01')).toBeTruthy();
    expect(screen.getByText('qqq-01')).toBeTruthy();
    expect(screen.getByText(/Opening Range Breakout/)).toBeTruthy();
    expect(container.querySelectorAll('app-asset-identity')).toHaveLength(2);
  });

  it('filters by search term', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'spy-01', symbol: 'SPY' }),
      fakeBot({ strategy_instance_id: 'qqq-01', symbol: 'QQQ' }),
    ];
    await renderRoster(bots);

    const search = screen.getByRole('searchbox', { name: 'Search bots' });
    fireEvent.input(search, { target: { value: 'qqq' } });

    expect(await screen.findByText('qqq-01')).toBeTruthy();
    expect(screen.queryByText('spy-01')).toBeNull();
  });

  it('filters by the human strategy label', async () => {
    const bots = [
      fakeBot({
        strategy_instance_id: 'validation-01',
        strategy_key: 'deployment_validation',
        strategy_label: 'Opening Range Breakout',
      }),
      fakeBot({
        strategy_instance_id: 'other-01',
        strategy_key: 'ema_crossover_signal',
        strategy_label: 'Moving Average Crossover',
      }),
    ];
    await renderRoster(bots);

    fireEvent.input(screen.getByRole('searchbox', { name: 'Search bots' }), {
      target: { value: 'opening range breakout' },
    });

    expect(await screen.findByText('validation-01')).toBeTruthy();
    expect(screen.queryByText('other-01')).toBeNull();
  });

  it('filters bots that need attention', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'normal-bot' }),
      fakeBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
    ];
    await renderRoster(bots);

    fireEvent.click(screen.getByRole('button', { name: 'Needs attention' }));

    expect(await screen.findByText('urgent-bot')).toBeTruthy();
    expect(screen.queryByText('normal-bot')).toBeNull();
  });

  it('renders a dedicated empty-fleet state', async () => {
    await renderRoster([]);
    expect(await screen.findByText(/No Alpaca bots yet/i)).toBeTruthy();
  });

  it('renders a dedicated empty-filter state', async () => {
    await renderRoster([fakeBot()]);
    fireEvent.input(screen.getByRole('searchbox', { name: 'Search bots' }), {
      target: { value: 'missing' },
    });
    expect(await screen.findByText(/No bots match these filters/i)).toBeTruthy();
  });

  it('renders one semantic six-column roster', async () => {
    await renderRoster([fakeBot()]);

    expect(screen.getAllByRole('columnheader').map((header) => header.textContent?.trim())).toEqual([
      'Bot',
      'State',
      'Exposure',
      'Today',
      'Last activity',
      'Action',
    ]);
    expect(screen.getByText(/Deployment Validation/)).toBeTruthy();
    expect(screen.getByText('Running under Account Clerk custody.')).toBeTruthy();
  });

  it('sorts attention bots first', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'normal-bot', needs_attention: false }),
      fakeBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
    ];
    await renderRoster(bots);

    // Wait for both to render, then check order in page text.
    await screen.findByText('urgent-bot');
    await screen.findByText('normal-bot');
    const bodyText = document.body.textContent ?? '';
    const urgentIdx = bodyText.indexOf('urgent-bot');
    const normalIdx = bodyText.indexOf('normal-bot');
    expect(urgentIdx).toBeLessThan(normalIdx);
  });

  it('renders the server-presented row action as pending and disabled', async () => {
    await renderRoster(
      [fakeBot({ row_action: fakeRowAction('stop') })],
      new Set(['spy-01']),
    );

    const action = await screen.findByRole('button', { name: 'Stop spy-01' });
    if (!(action instanceof HTMLButtonElement)) throw new Error('Expected a button action.');
    expect(action.getAttribute('aria-busy')).toBe('true');
    expect(action.disabled).toBe(true);
    expect(action.textContent).toContain('Stopping…');
  });
});
