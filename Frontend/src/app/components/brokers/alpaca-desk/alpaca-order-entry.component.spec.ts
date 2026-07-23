import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerOrderRequest, OrderSubmitResult } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';

function ackedResult(orderRef = 'manual/desk/v1:abc123'): OrderSubmitResult {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    results: [
      {
        status: 'acked',
        order_ref: orderRef,
        intent_id: 'abc123',
        order: {
          broker: 'alpaca',
          order_id: 'broker-order-1',
          client_order_id: orderRef,
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
        },
        error: null,
      },
    ],
  };
}

async function renderPanel(
  submitOrder: (broker: string, request: BrokerOrderRequest) => Promise<OrderSubmitResult>,
) {
  return render(AlpacaOrderEntryComponent, {
    providers: [{ provide: BrokersService, useValue: { submitOrder } }],
  });
}

async function fillFirstLeg(symbol: string, quantity: string): Promise<void> {
  fireEvent.click(screen.getByRole('button', { name: 'Add equity leg' }));
  fireEvent.input(await screen.findByLabelText('Leg 1 symbol'), {
    target: { value: symbol },
  });
  // p-inputNumber renders an inner <input>; drive it directly by its aria-label.
  const qtyInput = await screen.findByLabelText('Leg 1 quantity');
  fireEvent.input(qtyInput, { target: { value: quantity } });
  fireEvent.blur(qtyInput);
}

describe('AlpacaOrderEntryComponent', () => {
  it('adds an equity leg, previews, confirms, and submits the right payload', async () => {
    const submitOrder = vi.fn().mockResolvedValue(ackedResult());
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');

    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await vi.waitFor(() => expect(submitOrder).toHaveBeenCalledTimes(1));
    const [broker, request] = submitOrder.mock.calls[0];
    expect(broker).toBe('alpaca');
    expect(request).toEqual({
      operator: 'desk',
      legs: [{ symbol: 'SPY', side: 'buy', quantity: 2, order_type: 'market' }],
    });

    // The per-leg result renders (acked), with the opaque order_ref shown exactly.
    expect(await screen.findByText('manual/desk/v1:abc123')).toBeTruthy();
  });

  it('disables the option-leg button with a coming-soon hint', async () => {
    await renderPanel(vi.fn());

    const optionButton = screen.getByRole('button', { name: 'Add option leg' });
    expect(optionButton.hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('Option legs are coming in 2b.')).toBeTruthy();
  });

  it('renders a typed per-leg failure without a raw error', async () => {
    const failing: OrderSubmitResult = {
      broker: 'alpaca',
      account_id: 'PA1',
      results: [
        {
          status: 'failed',
          order_ref: 'manual/desk/v1:zzz',
          intent_id: 'zzz',
          order: null,
          error: { message: 'insufficient buying power', why: 'HTTP 422' },
        },
      ],
    };
    const submitOrder = vi.fn().mockResolvedValue(failing);
    await renderPanel(submitOrder);

    await fillFirstLeg('spy', '2');
    fireEvent.click(screen.getByRole('button', { name: /Preview order/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    const results = await screen.findByLabelText('Submission results');
    expect(within(results).getByText(/insufficient buying power/)).toBeTruthy();
  });
});
