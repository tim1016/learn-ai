import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { BrokerActivity } from '../../../api/alpaca.types';
import { AlpacaTraderActivityTableComponent } from './alpaca-trader-activity-table.component';

describe('AlpacaTraderActivityTableComponent', () => {
  it('renders the approved compact columns and instrument identities', async () => {
    await render(AlpacaTraderActivityTableComponent, {
      inputs: { activities: [activity('newer', 'NVDA', 20), activity('older', 'SPY', 10)] },
    });

    const table = screen.getByRole('table', { name: "Today's account activity" });
    for (const column of [
      'Time (local)', 'Instrument', 'Activity', 'Side', 'Quantity', 'Price', 'Net amount',
    ]) {
      expect(within(table).getByText(column)).toBeTruthy();
    }
    expect(screen.getAllByTitle('NVDA').length).toBeGreaterThan(0);
    expect(screen.getAllByTitle('SPY').length).toBeGreaterThan(0);
  });

  it('searches client-side and keeps technical references behind row details', async () => {
    await render(AlpacaTraderActivityTableComponent, {
      inputs: { activities: [activity('newer', 'NVDA', 20), activity('older', 'SPY', 10)] },
    });

    fireEvent.input(screen.getByRole('searchbox', { name: "Search today's activity" }), {
      target: { value: 'SPY' },
    });
    await waitFor(() => expect(screen.queryByTitle('NVDA')).toBeNull());
    expect(screen.getByTitle('SPY')).toBeTruthy();
    expect(screen.queryByText('order-older')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Show activity details for SPY Fill' }));
    expect(screen.getByText('order-older')).toBeTruthy();
    expect(screen.getByText('Observed (local)')).toBeTruthy();
  });
});

function activity(id: string, symbol: string, occurredAtMs: number): BrokerActivity {
  return {
    broker: 'alpaca',
    activity_id: id,
    native_order_id: `order-${id}`,
    activity_type: 'FILL',
    category: 'trade_activity',
    symbol,
    side: 'buy',
    quantity: 2,
    price: 100,
    net_amount: -200,
    occurred_at_ms: occurredAtMs,
    observed_at_ms: occurredAtMs,
  };
}
