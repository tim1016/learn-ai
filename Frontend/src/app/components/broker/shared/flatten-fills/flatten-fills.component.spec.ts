import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { RecentFillView } from '../../v2-panel/lib/broker-v2-panel.types';
import { FlattenFillsComponent } from './flatten-fills.component';

function fill(overrides: Partial<RecentFillView> = {}): RecentFillView {
  return {
    order_ref: 'learn-ai/sid-001/v1:flatten',
    symbol: 'SPY',
    side: 'sell',
    quantity: 10,
    price: 511.2,
    filled_at_ms: 1_753_794_000_000,
    simulated: false,
    authority_account_id: 'PA-TEST',
    authority_kind: 'real_paper',
    event_key: 'execution:flatten-1',
    slippage_reference_price: 512.31,
    slippage_bps: 21.7,
    // 10 shares × $1.11 below the bid the Clerk priced against.
    slippage_cost: 11.1,
    ...overrides,
  };
}

describe('FlattenFillsComponent', () => {
  it('reads each fill against the quote the limit was priced from', async () => {
    await render(FlattenFillsComponent, { inputs: { fills: [fill()] } });

    const row = within(screen.getByRole('table')).getAllByRole('row')[1];
    expect(within(row).getByText(/\$511\.20/)).toBeTruthy();
    expect(within(row).getByText(/\$512\.31/)).toBeTruthy();
    expect(within(row).getByText(/21\.7 bps \(\$11\.10\)/)).toBeTruthy();
  });

  it('shows a cover against the ask, and a fill better than the reference as negative', async () => {
    await render(FlattenFillsComponent, {
      inputs: {
        fills: [fill({
          side: 'buy',
          price: 511.0,
          slippage_reference_price: 512.0,
          slippage_bps: -19.5,
          slippage_cost: -10,
        })],
      },
    });

    const row = within(screen.getByRole('table')).getAllByRole('row')[1];
    expect(within(row).getByText(/-19\.5 bps \(-\$10\.00\)/)).toBeTruthy();
  });

  it('lists only fills the Clerk measured, never an unpriced one', async () => {
    await render(FlattenFillsComponent, {
      inputs: {
        fills: [
          fill(),
          fill({
            order_ref: 'other',
            event_key: 'execution:unpriced',
            slippage_bps: null,
            slippage_reference_price: null,
            slippage_cost: null,
          }),
        ],
      },
    });

    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(2);
  });
});
