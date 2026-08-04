import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerOrder, OrderCancelResult } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaOrdersTableComponent } from './alpaca-orders-table.component';

function fakeOrder(overrides: Partial<BrokerOrder> = {}): BrokerOrder {
  return {
    broker: 'alpaca',
    order_id: 'o-1',
    client_order_id: 'manual/desk/v1:abc',
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
    updated_at_ms: 1_700_000_000_500,
    filled_at_ms: 1_700_000_000_500,
    canceled_at_ms: null,
    expired_at_ms: null,
    events: [],
    observed_at_ms: 1_700_000_000_000,
    fill_latency_seconds: 0.5,
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
  listOrders: () => Promise<BrokerOrder[]>,
  cancelOrder?: (broker: string, orderId: string) => Promise<OrderCancelResult>,
) {
  return render(AlpacaOrdersTableComponent, {
    providers: [{ provide: BrokersService, useValue: { listOrders, cancelOrder } }],
  });
}

describe('AlpacaOrdersTableComponent', () => {
  it('renders one searchable table row per order with shared entity identities and fill latency', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeOrder({ order_id: 'amd-1', symbol: 'AMD' }),
        fakeOrder({ order_id: 'amzn-1', symbol: 'AMZN', fill_latency_seconds: 1.25 }),
      ]),
    );

    expect(await screen.findByRole('table')).toBeTruthy();
    expect(screen.getByLabelText('Alpaca transaction history, newest orders first')).toBeTruthy();
    expect(screen.getByText('AMD')).toBeTruthy();
    expect(screen.getByText('AMZN')).toBeTruthy();
    expect(screen.getByText('0.500 s')).toBeTruthy();
    expect(screen.getByText('1.250 s')).toBeTruthy();
  });

  it('shows the most recently submitted order first by default', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeOrder({ order_id: 'old', symbol: 'AAPL', submitted_at_ms: 100 }),
        fakeOrder({ order_id: 'new', symbol: 'AMD', submitted_at_ms: 300 }),
        fakeOrder({ order_id: 'middle', symbol: 'AMZN', submitted_at_ms: 200 }),
      ]),
    );

    await screen.findByText('AMD');
    const bodyRows = screen.getAllByRole('row').slice(1);
    expect(within(bodyRows[0]).getByText('AMD')).toBeTruthy();
    expect(within(bodyRows[1]).getByText('AMZN')).toBeTruthy();
    expect(within(bodyRows[2]).getByText('AAPL')).toBeTruthy();
  });

  it('filters by symbol/reference search and by order type', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeOrder({ order_id: 'amd-market', symbol: 'AMD', order_type: 'market' }),
        fakeOrder({ order_id: 'amzn-limit', symbol: 'AMZN', order_type: 'limit' }),
      ]),
    );

    const searchInput = await screen.findByRole('searchbox', { name: 'Search transactions' });
    fireEvent.input(searchInput, { target: { value: 'AMZN' } });
    await waitFor(() => expect(screen.queryByText('AMD')).toBeNull());
    expect(screen.getByText('AMZN')).toBeTruthy();

    fireEvent.input(searchInput, { target: { value: '' } });
    const orderType = screen.getByRole('combobox', { name: /Order type/i });
    fireEvent.change(orderType, { target: { value: 'market' } });
    await waitFor(() => expect(screen.queryByText('AMZN')).toBeNull());
    expect(screen.getByText('AMD')).toBeTruthy();
  });

  it('renders the rows-per-page overlay outside the scrollable table', async () => {
    await renderTable(() =>
      Promise.resolve(
        Array.from({ length: 11 }, (_, index) =>
          fakeOrder({ order_id: `order-${index}`, symbol: `ASSET${index}` }),
        ),
      ),
    );

    fireEvent.click(await screen.findByRole('combobox', { name: 'Rows per page' }));

    await waitFor(() => {
      const overlayRoot = [...document.body.children].find(
        (element) => element.matches('.p-overlay') && element.querySelector('.p-select-overlay'),
      );
      expect(overlayRoot).toBeTruthy();
    });
  });

  it('shows broker-verifiable order references in the information popover', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeOrder({
          order_id: 'alpaca-order-0009',
          client_order_id: 'manual/desk/v1:verify-me',
          symbol: 'AMD',
        }),
      ]),
    );

    fireEvent.click(await screen.findByRole('button', { name: 'More information for AMD order' }));

    expect(await screen.findByText('alpaca-order-0009')).toBeTruthy();
    expect(screen.getByText('manual/desk/v1:verify-me')).toBeTruthy();
    expect(screen.getByText(/Use the Alpaca order ID to verify/)).toBeTruthy();
  });

  it('renders honest-empty transaction history, distinct from error', async () => {
    await renderTable(() => Promise.resolve([]));

    expect(await screen.findByText('No recent transactions.')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('renders an error state when Alpaca is unreachable', async () => {
    await renderTable(() => Promise.reject(new Error('unreachable')));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain("Couldn't reach Alpaca");
  });

  it('offers Cancel only on still-working orders', async () => {
    await renderTable(() =>
      Promise.resolve([
        fakeOrder({ order_id: 'o-open', symbol: 'MSFT', status: 'new' }),
        fakeOrder({ order_id: 'o-day-end', symbol: 'GTC', status: 'done_for_day' }),
        fakeOrder({ order_id: 'o-done', symbol: 'AAPL', status: 'filled' }),
      ]),
    );

    expect(await screen.findByRole('button', { name: 'Cancel order for MSFT' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel order for GTC' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel order for AAPL' })).toBeNull();
  });

  it('cancels by order_id and refreshes after success', async () => {
    const cancelOrder = vi
      .fn<(broker: string, orderId: string) => Promise<OrderCancelResult>>()
      .mockResolvedValue(ackedCancel('o-open'));
    const listOrders = vi
      .fn<() => Promise<BrokerOrder[]>>()
      .mockResolvedValue([fakeOrder({ order_id: 'o-open', symbol: 'MSFT', status: 'new' })]);

    await renderTable(listOrders, cancelOrder);
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel order for MSFT' }));

    expect(cancelOrder).toHaveBeenCalledWith('alpaca', 'o-open');
    await waitFor(() => expect(listOrders).toHaveBeenCalledTimes(2));
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
    const listOrders = vi
      .fn<() => Promise<BrokerOrder[]>>()
      .mockResolvedValue([fakeOrder({ order_id: 'o-open', symbol: 'MSFT', status: 'new' })]);

    await renderTable(listOrders, cancelOrder);
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel order for MSFT' }));

    expect(await screen.findByText('order is not cancelable')).toBeTruthy();
    expect(listOrders).toHaveBeenCalledTimes(1);
  });
});
