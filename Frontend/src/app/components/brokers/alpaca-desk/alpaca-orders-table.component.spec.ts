import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { BrokerOrder } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaOrdersTableComponent } from './alpaca-orders-table.component';

function fakeOrder(overrides: Partial<BrokerOrder> = {}): BrokerOrder {
  return {
    broker: 'alpaca',
    order_id: 'o-1',
    client_order_id: null,
    symbol: 'AAPL',
    asset_class: 'us_equity',
    side: 'buy',
    order_type: 'market',
    time_in_force: 'day',
    quantity: 10,
    filled_quantity: 10,
    limit_price: null,
    stop_price: null,
    filled_avg_price: 135.8,
    status: 'filled',
    submitted_at_ms: 1_700_000_000_000,
    created_at_ms: 1_700_000_000_000,
    updated_at_ms: 1_700_000_000_000,
    filled_at_ms: 1_700_000_000_500,
    canceled_at_ms: null,
    expired_at_ms: null,
    events: [],
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

async function renderTable(listOrders: () => Promise<BrokerOrder[]>) {
  return render(AlpacaOrdersTableComponent, {
    providers: [{ provide: BrokersService, useValue: { listOrders } }],
  });
}

describe('AlpacaOrdersTableComponent', () => {
  it('renders a row per order with the status routed through receiptLabel', async () => {
    await renderTable(() => Promise.resolve([fakeOrder({ order_id: 'o-9', status: 'filled' })]));

    expect(await screen.findByText('AAPL')).toBeTruthy();
    // receiptLabel title-cases the status code ("filled" → "Filled"); query by
    // cell role to disambiguate from the "Filled" (filled-qty) column header.
    expect(screen.getByRole('cell', { name: 'Filled' })).toBeTruthy();
  });

  it('renders honest-empty ("no recent orders"), distinct from error', async () => {
    await renderTable(() => Promise.resolve([]));

    expect(await screen.findByText('No recent orders.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders an error state when Alpaca is unreachable', async () => {
    await renderTable(() => Promise.reject(new Error('unreachable')));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Couldn't reach Alpaca");
  });
});
