import { render, screen } from '@testing-library/angular';
import { describe, it, expect } from 'vitest';
import { TradesTodayListComponent } from './trades-today-list.component';
import type { ChartFillMarker } from '../lib/broker-v2-panel.types';

const BUY_FILL: ChartFillMarker = {
  filled_at_ms: 1_753_800_000_000,
  side: 'buy',
  quantity: 100,
  price: 512.3,
  order_ref: 'ord-buy-001',
};

const SELL_FILL: ChartFillMarker = {
  filled_at_ms: 1_753_801_620_000,
  side: 'sell',
  quantity: 100,
  price: 512.9,
  order_ref: 'ord-sell-001',
};

describe('TradesTodayListComponent', () => {
  it('shows no-trades message when fills are empty', async () => {
    await render(TradesTodayListComponent, {
      inputs: { fills: [], feeFidelity: 'none', tradingDateMs: null },
    });

    expect(screen.getByText('No trades today.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('renders one row per fill when fills are present', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [BUY_FILL, SELL_FILL],
        feeFidelity: 'per_fill',
        tradingDateMs: null,
        realizedPnlToday: 60,
        openPnl: null,
      },
    });

    const rows = screen.getAllByRole('row');
    // header row + 2 fill rows
    expect(rows.length).toBe(3);
  });

  it('shows "Fees not reported" when feeFidelity is none', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [BUY_FILL, SELL_FILL],
        feeFidelity: 'none',
        tradingDateMs: null,
        realizedPnlToday: 60,
        openPnl: null,
      },
    });

    expect(screen.getByText('Fees not reported')).toBeTruthy();
  });

  it('displays backend-provided realized P&L', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [BUY_FILL, SELL_FILL],
        feeFidelity: 'per_fill',
        tradingDateMs: null,
        realizedPnlToday: 60.0,
        openPnl: null,
      },
    });

    expect(screen.getByLabelText('Realized P&L today').textContent).toContain('+$60.00');
  });

  it('shows em dash for null open P&L', async () => {
    await render(TradesTodayListComponent, {
      inputs: {
        fills: [BUY_FILL],
        feeFidelity: 'per_fill',
        tradingDateMs: null,
        realizedPnlToday: 0,
        openPnl: null,
      },
    });

    expect(screen.getByLabelText('Open P&L').textContent).toContain('—');
  });
});
