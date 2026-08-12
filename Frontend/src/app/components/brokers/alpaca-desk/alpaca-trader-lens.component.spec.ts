import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  BrokerAccountSnapshot,
  BrokerActivity,
  BrokerPosition,
} from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaTraderLensComponent } from './alpaca-trader-lens.component';

function account(): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 1_500,
    equity: 10_250,
    buying_power: 20_500,
    portfolio_value: 10_250,
    long_market_value: 8_750,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: null,
    observed_at_ms: 1_700_000_000_000,
  };
}

function positions(): BrokerPosition[] {
  return [
    {
      broker: 'alpaca',
      symbol: 'SPY',
      asset_id: 'spy',
      asset_class: 'us_equity',
      quantity: 10,
      side: 'long',
      average_entry_price: 500,
      market_value: 5_100,
      cost_basis: 5_000,
      current_price: 510,
      unrealized_pl: 100,
      unrealized_plpc: 0.02,
      observed_at_ms: 1_700_000_000_000,
    },
    {
      broker: 'alpaca',
      symbol: 'NVDA',
      asset_id: 'nvda',
      asset_class: 'us_equity',
      quantity: 5,
      side: 'long',
      average_entry_price: 800,
      market_value: 4_000,
      cost_basis: 4_000,
      current_price: 800,
      unrealized_pl: 0,
      unrealized_plpc: 0,
      observed_at_ms: 1_700_000_000_000,
    },
  ];
}

function activities(): BrokerActivity[] {
  return [
    {
      broker: 'alpaca',
      activity_id: 'fill-newer',
      native_order_id: 'order-2',
      activity_type: 'FILL',
      category: 'trade_activity',
      symbol: 'NVDA',
      side: 'buy',
      quantity: 5,
      price: 800,
      net_amount: -4_000,
      occurred_at_ms: 1_700_000_000_200,
      observed_at_ms: 1_700_000_000_200,
    },
    {
      broker: 'alpaca',
      activity_id: 'fill-older',
      native_order_id: 'order-1',
      activity_type: 'FILL',
      category: 'trade_activity',
      symbol: 'SPY',
      side: 'sell',
      quantity: 10,
      price: 510,
      net_amount: 5_100,
      occurred_at_ms: 1_700_000_000_100,
      observed_at_ms: 1_700_000_000_100,
    },
  ];
}

function brokers() {
  return {
    getAccount: vi.fn().mockResolvedValue(account()),
    listPositions: vi.fn().mockResolvedValue(positions()),
    listActivities: vi.fn().mockResolvedValue(activities()),
  };
}

async function renderLens(broker = brokers()) {
  await render(AlpacaTraderLensComponent, {
    providers: [
      AlpacaDeskAccountDataService,
      { provide: BrokersService, useValue: broker },
    ],
  });
  return broker;
}

describe('AlpacaTraderLensComponent', () => {
  it("renders today's live positions, fill count, and instrument identities", async () => {
    const broker = await renderLens();

    expect(await screen.findAllByTitle('SPY')).not.toHaveLength(0);
    const hero = screen.getByLabelText('Today at a glance');
    expect(within(hero).getAllByText('2')).toHaveLength(2);
    expect(screen.getByText('Fills today')).toBeTruthy();
    expect(within(hero).getByText('Open positions')).toBeTruthy();
    expect(within(hero).getByText('Realized P&L today')).toBeTruthy();
    expect(within(hero).getByText('Reconciled account attribution is not available yet.')).toBeTruthy();
    expect(screen.getByRole('list', { name: 'Today at the desk activity' })).toBeTruthy();
    expect(screen.getAllByTitle('NVDA')).not.toHaveLength(0);
    expect(broker.listActivities).toHaveBeenCalledWith('alpaca', expect.objectContaining({ limit: 100 }));
  });

  it('shows an honest future-history placeholder when a wider scope is selected', async () => {
    await renderLens();

    fireEvent.click(screen.getByRole('button', { name: '30D' }));

    expect(screen.getByRole('button', { name: '30D' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('heading', { name: '30D account history' })).toBeTruthy();
    expect(screen.getByText(/Broker portfolio history for this window/)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Today at the desk' })).toBeNull();
  });

  it('distinguishes an unavailable activity feed from an empty day', async () => {
    const broker = brokers();
    broker.listActivities.mockRejectedValue(new Error('unreachable'));
    await renderLens(broker);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Today's account activity is unavailable");
    expect(screen.queryByText('No account activity has been recorded today.')).toBeNull();
  });
});
