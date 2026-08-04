import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerOrder, CustodyDiagnosis, OrderSubmitResult } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskComponent } from './alpaca-desk.component';

function acknowledgedSubmission(): OrderSubmitResult {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    results: [
      {
        status: 'acked',
        order_ref: 'manual/desk/v1:spy',
        intent_id: 'intent-spy',
        order: null,
        error: null,
      },
    ],
  };
}

function submittedSpyOrder(): BrokerOrder {
  return {
    broker: 'alpaca',
    order_id: 'broker-order-spy',
    client_order_id: 'manual/desk/v1:spy',
    symbol: 'SPY',
    asset_class: 'us_equity',
    side: 'buy',
    order_type: 'market',
    time_in_force: 'day',
    quantity: 2,
    filled_quantity: 0,
    limit_price: null,
    stop_price: null,
    filled_avg_price: null,
    status: 'accepted',
    submitted_at_ms: 1_700_000_000_000,
    created_at_ms: 1_700_000_000_000,
    updated_at_ms: 1_700_000_000_000,
    filled_at_ms: null,
    canceled_at_ms: null,
    expired_at_ms: null,
    events: [],
    observed_at_ms: 1_700_000_000_000,
    fill_latency_seconds: null,
  };
}

function inSyncDiagnosis(): CustodyDiagnosis {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    in_sync: true,
    observed_at_ms: 1,
    snapshot_version: 'v1',
    resolution_posture: 'paper',
    resolvable: false,
    blocked_reason: null,
    divergences: [],
    resolution_plan: [],
  };
}

function brokerService(overrides: Partial<BrokersService> = {}) {
  return {
    getAccount: vi.fn().mockResolvedValue(undefined),
    listPositions: vi.fn().mockResolvedValue([]),
    listOrders: vi.fn().mockResolvedValue([]),
    submitOrder: vi.fn(),
    getClerkStatus: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      hold: { active: false, reason_code: null, reason: null, since_ms: null },
      latest_reconciliation: null,
      outstanding_intents: 0,
      observed_at_ms: 1,
    }),
    getCustodyDiagnosis: vi.fn().mockResolvedValue(inSyncDiagnosis()),
    ...overrides,
  };
}

describe('AlpacaDeskComponent', () => {
  it('renders the Alpaca desk heading and new-order action', async () => {
    await render(AlpacaDeskComponent, {
      providers: [{ provide: BrokersService, useValue: brokerService() }],
    });

    expect(screen.getByRole('heading', { name: /Alpaca/i })).toBeTruthy();
    expect(screen.getByText('Broker desk')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Create a new Alpaca order' })).toBeTruthy();
  });

  it('refreshes transaction history after a successful submission', async () => {
    const listOrders = vi
      .fn<() => Promise<BrokerOrder[]>>()
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([submittedSpyOrder()]);
    const submitOrder = vi.fn().mockResolvedValue(acknowledgedSubmission());

    await render(AlpacaDeskComponent, {
      providers: [
        {
          provide: BrokersService,
          useValue: brokerService({ listOrders, submitOrder }),
        },
      ],
    });

    await screen.findByText('No recent transactions.');
    fireEvent.click(screen.getByRole('button', { name: 'Create a new Alpaca order' }));
    fireEvent.input(await screen.findByLabelText('Leg 1 symbol'), { target: { value: 'SPY' } });
    const quantity = screen.getByLabelText('Leg 1 quantity');
    fireEvent.input(quantity, { target: { value: '2' } });
    fireEvent.change(quantity, { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await waitFor(() => expect(listOrders).toHaveBeenCalledTimes(2));
    const transactionHistory = screen.getByLabelText(
      'Alpaca transaction history, newest orders first',
    );
    expect(within(transactionHistory).getByText('SPY')).toBeTruthy();
  });
});
