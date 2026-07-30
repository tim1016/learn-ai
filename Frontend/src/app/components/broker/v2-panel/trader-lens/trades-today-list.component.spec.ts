import { render, screen } from '@testing-library/angular';
import { describe, it, expect } from 'vitest';
import { TradesTodayListComponent } from './trades-today-list.component';
import { pairFills } from './trades-today-list.component';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';

const BUY_FILL: ChartFillMarker = {
  filled_at_ms: 1_753_800_000_000, // 2025-07-29 09:40:00 UTC (approx)
  side: 'buy',
  quantity: 100,
  price: 512.30,
  order_ref: 'ord-buy-001',
};

const SELL_FILL: ChartFillMarker = {
  filled_at_ms: 1_753_801_620_000, // 27 minutes later
  side: 'sell',
  quantity: 100,
  price: 512.90,
  order_ref: 'ord-sell-001',
};

describe('pairFills()', () => {
  it('pairs a buy + sell into one trade', () => {
    const pairs = pairFills([BUY_FILL, SELL_FILL]);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].exitFill).not.toBeNull();
    expect(pairs[0].realizedPnl).toBeCloseTo(60, 4);
    expect(pairs[0].holdDurationMs).toBe(SELL_FILL.filled_at_ms - BUY_FILL.filled_at_ms);
  });

  it('marks an open buy with null exit', () => {
    const pairs = pairFills([BUY_FILL]);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].exitFill).toBeNull();
    expect(pairs[0].realizedPnl).toBeNull();
  });

  it('returns empty array for no fills', () => {
    expect(pairFills([])).toHaveLength(0);
  });
});

describe('TradesTodayListComponent', () => {
  it('shows "Fees not reported" when fee_fidelity is none', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [BUY_FILL, SELL_FILL],
        feeFidelity: 'none',
        sessionPolicy: 'RTH',
        tradingDateMs: null,
      },
    });

    // Fees not reported must appear, never $0.00 for the fee column
    const feesNotReportedElements = screen.getAllByText('Fees not reported');
    expect(feesNotReportedElements.length).toBeGreaterThan(0);
  });

  it('shows no-trades message when fills are empty', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [],
        feeFidelity: 'none',
        sessionPolicy: '',
        tradingDateMs: null,
      },
    });

    expect(screen.getByText('No trades today.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('renders one row per trade pair', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [BUY_FILL, SELL_FILL],
        feeFidelity: 'none',
        sessionPolicy: '',
        tradingDateMs: null,
      },
    });

    const rows = screen.getAllByRole('row');
    // header row + 1 trade row
    expect(rows.length).toBe(2);
  });
});
