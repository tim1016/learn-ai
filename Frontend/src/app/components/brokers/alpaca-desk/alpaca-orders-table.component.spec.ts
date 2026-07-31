import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerOrder, BrokerOrderGroup, OrderCancelResult } from '../../../api/alpaca.types';
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

function ackedCancel(orderId: string): OrderCancelResult {
  return {
    broker: 'alpaca',
    account_id: 'PA-1',
    order_id: orderId,
    status: 'acked',
    owned: true,
    order_ref: 'manual/desk/v1:abc',
    error: null,
  };
}

async function renderTable(
  listOrderGroups: () => Promise<BrokerOrderGroup[]>,
  cancelOrder?: (broker: string, orderId: string) => Promise<OrderCancelResult>,
) {
  return render(AlpacaOrdersTableComponent, {
    providers: [{ provide: BrokersService, useValue: { listOrderGroups, cancelOrder } }],
  });
}

function fakeGroup(
  symbol: string,
  orders: BrokerOrder[],
  overrides: Partial<BrokerOrderGroup> = {},
): BrokerOrderGroup {
  return {
    symbol,
    orders,
    order_count: orders.length,
    working_order_count: 0,
    gross_requested_quantity: 10,
    gross_filled_quantity: 10,
    gross_working_quantity: 0,
    ...overrides,
  };
}

describe('AlpacaOrdersTableComponent', () => {
  it('renders a row per order with the status routed through receiptLabel', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeGroup('AAPL', [fakeOrder({ order_id: 'o-9', status: 'filled' })]),
      ]),
    );

    expect(await screen.findByText('AAPL')).toBeTruthy();
    // receiptLabel title-cases the status code ("filled" → "Filled").
    expect(screen.getAllByText('Filled').length).toBeGreaterThan(0);
  });

  it('renders honest-empty ("no recent orders"), distinct from error', async () => {
    await renderTable(() => Promise.resolve([]));

    expect(await screen.findByText('No recent orders.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows three recent orders per symbol until the group is expanded', async () => {
    const orders = Array.from({ length: 5 }, (_, index) =>
      fakeOrder({ order_id: `o-${index}`, submitted_at_ms: 1_700_000_000_000 + index }),
    );
    await renderTable(() => Promise.resolve([fakeGroup('AAPL', orders)]));

    expect(await screen.findAllByRole('listitem')).toHaveLength(3);
    fireEvent.click(screen.getByRole('button', { name: 'Show 2 more' }));
    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(5));
    expect(screen.getByRole('button', { name: 'Show recent only' })).toBeTruthy();
  });

  it('renders an error state when Alpaca is unreachable', async () => {
    await renderTable(() => Promise.reject(new Error('unreachable')));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Couldn't reach Alpaca");
  });

  it('offers a Cancel action only on a still-working (cancelable) order', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeGroup('MSFT', [fakeOrder({ order_id: 'o-open', symbol: 'MSFT', status: 'new' })], {
          working_order_count: 1,
          gross_working_quantity: 10,
        }),
        fakeGroup('GTC', [fakeOrder({ order_id: 'o-day-end', symbol: 'GTC', status: 'done_for_day' })], {
          working_order_count: 1,
          gross_working_quantity: 10,
        }),
        fakeGroup('AAPL', [fakeOrder({ order_id: 'o-done', symbol: 'AAPL', status: 'filled' })]),
      ]),
    );

    // The working order gets a labeled Cancel button; the filled one does not.
    expect(await screen.findByRole('button', { name: 'Cancel order for MSFT' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel order for GTC' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel order for AAPL' })).toBeNull();
  });

  it('cancels by order_id and refreshes the list after success', async () => {
    const cancelOrder = vi
      .fn<(broker: string, orderId: string) => Promise<OrderCancelResult>>()
      .mockResolvedValue(ackedCancel('o-open'));
    const listOrderGroups = vi
      .fn<() => Promise<BrokerOrderGroup[]>>()
      .mockResolvedValue([
        fakeGroup('MSFT', [fakeOrder({ order_id: 'o-open', symbol: 'MSFT', status: 'new' })]),
      ]);

    await renderTable(listOrderGroups, cancelOrder);

    const button = await screen.findByRole('button', { name: 'Cancel order for MSFT' });
    fireEvent.click(button);

    // Cancel is called with the opaque broker order_id.
    expect(cancelOrder).toHaveBeenCalledWith('alpaca', 'o-open');
    // The list reloads after a successful cancel (initial load + reload).
    await waitFor(() => expect(listOrderGroups).toHaveBeenCalledTimes(2));
  });

  it('surfaces a typed cancel failure inline without refreshing', async () => {
    const cancelOrder = vi
      .fn<(broker: string, orderId: string) => Promise<OrderCancelResult>>()
      .mockResolvedValue({
        ...ackedCancel('o-open'),
        status: 'failed',
        owned: false,
        order_ref: null,
        error: { message: 'order is not cancelable', why: 'HTTP 422' },
      });
    const listOrderGroups = vi
      .fn<() => Promise<BrokerOrderGroup[]>>()
      .mockResolvedValue([
        fakeGroup('MSFT', [fakeOrder({ order_id: 'o-open', symbol: 'MSFT', status: 'new' })]),
      ]);

    await renderTable(listOrderGroups, cancelOrder);

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel order for MSFT' }));

    expect(await screen.findByText('order is not cancelable')).toBeTruthy();
    // A failed cancel does not reload the list (only the initial load ran).
    expect(listOrderGroups).toHaveBeenCalledTimes(1);
  });
});
