import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { BrokerPosition } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';

function fakePosition(overrides: Partial<BrokerPosition> = {}): BrokerPosition {
  return {
    broker: 'alpaca',
    symbol: 'AAPL',
    asset_id: 'a-1',
    asset_class: 'us_equity',
    quantity: 10,
    side: 'long',
    average_entry_price: 135.8,
    market_value: 1358.02,
    cost_basis: 1358,
    current_price: 135.8,
    unrealized_pl: 0.02,
    unrealized_plpc: 0,
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

async function renderTable(listPositions: () => Promise<BrokerPosition[]>) {
  return render(AlpacaPositionsTableComponent, {
    providers: [{ provide: BrokersService, useValue: { listPositions } }],
  });
}

describe('AlpacaPositionsTableComponent', () => {
  it('renders a row per position', async () => {
    await renderTable(() =>
      Promise.resolve([fakePosition({ symbol: 'AAPL' }), fakePosition({ symbol: 'TSLA' })]),
    );

    expect(await screen.findByText('AAPL')).toBeTruthy();
    expect(screen.getByText('TSLA')).toBeTruthy();
  });

  it('renders honest-empty ("no positions"), distinct from error', async () => {
    await renderTable(() => Promise.resolve([]));

    expect(await screen.findByText('No open positions.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders an error state when Alpaca is unreachable', async () => {
    await renderTable(() => Promise.reject(new Error('unreachable')));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Couldn't reach Alpaca");
  });
});
