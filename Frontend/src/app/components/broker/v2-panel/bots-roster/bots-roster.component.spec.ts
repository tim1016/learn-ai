import { render, screen, fireEvent } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import type { BotCatalogView, PanelProfile } from '../models/broker-v2-panel.types';
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
    status_label: 'Working',
    exposure: {},
    fills_today: 1,
    realized_pnl_today: 10,
    open_pnl: null,
    last_activity_at_ms: 1_700_000_000_000,
    needs_attention: false,
    ...overrides,
  };
}

function fakeProfile(supported: string[] = ['start', 'stop']): PanelProfile {
  return {
    broker: 'alpaca',
    fee_fidelity: 'none',
    flatten_supported: false,
    live_bars_supported: true,
    stations: [],
    supported_action_ids: supported as PanelProfile['supported_action_ids'],
  };
}

async function renderRoster(bots: BotCatalogView[], profile: PanelProfile = fakeProfile()) {
  return render(BotsRosterComponent, {
    providers: [provideRouter([])],
    componentInputs: {
      bots,
      profile,
      broker: 'alpaca',
    },
  });
}

describe('BotsRosterComponent', () => {
  it('renders all bot rows', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'spy-01', symbol: 'SPY' }),
      fakeBot({ strategy_instance_id: 'qqq-01', symbol: 'QQQ' }),
    ];
    await renderRoster(bots);

    expect(await screen.findByText('spy-01')).toBeTruthy();
    expect(screen.getByText('qqq-01')).toBeTruthy();
  });

  it('filters by search term', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'spy-01', symbol: 'SPY' }),
      fakeBot({ strategy_instance_id: 'qqq-01', symbol: 'QQQ' }),
    ];
    await renderRoster(bots);

    const search = screen.getByPlaceholderText(/Filter by bot ID/i);
    fireEvent.input(search, { target: { value: 'qqq' } });

    expect(await screen.findByText('qqq-01')).toBeTruthy();
    expect(screen.queryByText('spy-01')).toBeNull();
  });

  it('sorts attention bots first', async () => {
    const bots = [
      fakeBot({ strategy_instance_id: 'normal-bot', needs_attention: false }),
      fakeBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
    ];
    await renderRoster(bots);

    const rows = await screen.findAllByRole('row');
    // First data row (index 1 if header is row 0, or just check text order)
    const allText = rows.map((r) => r.textContent ?? '').join('|');
    const urgentIdx = allText.indexOf('urgent-bot');
    const normalIdx = allText.indexOf('normal-bot');
    expect(urgentIdx).toBeLessThan(normalIdx);
  });
});
