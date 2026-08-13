import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { FillTableComponent } from './fill-table.component';

describe('FillTableComponent', () => {
  it('renders timestamped buy and sell fills from the chart projection', async () => {
    await render(FillTableComponent, {
      inputs: {
        label: 'All fills today',
        fills: [
          { filled_at_ms: 1_753_800_000_000, side: 'buy', quantity: 2, price: 625.1, order_ref: 'buy-1' },
          { filled_at_ms: 1_753_800_005_000, side: 'sell', quantity: 1, price: 626.2, order_ref: 'sell-1' },
        ],
      },
    });

    expect(screen.getByRole('table', { name: 'All fills today' })).toBeTruthy();
    expect(screen.getByText('Buy')).toBeTruthy();
    expect(screen.getByText('Sell')).toBeTruthy();
    expect(screen.getByText('$625.10')).toBeTruthy();
    expect(screen.getByText('$626.20')).toBeTruthy();
  });
});
