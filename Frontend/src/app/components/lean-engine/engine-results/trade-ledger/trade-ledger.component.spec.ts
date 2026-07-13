import { provideZonelessChangeDetection } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { EngineTrade } from '../engine-results.component';
import { TradeLedgerComponent } from './trade-ledger.component';

function trade(tradeNumber: number): EngineTrade {
  const entry = Date.UTC(2026, 6, tradeNumber, 14, 30, 0);
  return {
    trade_number: tradeNumber,
    entry_time: entry,
    entry_price: 100 + tradeNumber,
    exit_time: entry + 3_600_000,
    exit_price: 101 + tradeNumber,
    quantity: 1,
    indicators: {},
    pnl_pts: tradeNumber % 2 === 0 ? 1 : -1,
    pnl_pct: tradeNumber % 2 === 0 ? 0.01 : -0.01,
    result: tradeNumber % 2 === 0 ? 'WIN' : 'LOSS',
    signal_reason: `signal-${tradeNumber}`,
  };
}

describe('TradeLedgerComponent', () => {
  it('shows the six most recent trades first and expands to the full trade history', async () => {
    await render(TradeLedgerComponent, {
      inputs: { trades: Array.from({ length: 7 }, (_, index) => trade(index + 1)) },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByRole('heading', { name: 'Trade history' })).toBeTruthy();
    expect(screen.getByText('Showing 6 of 7 closed trades · viewer-local time')).toBeTruthy();
    expect(screen.queryByText('signal-1')).toBeNull();
    expect(screen.getByText('Signal 7')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Open complete trade ledger' }));

    expect(screen.getByText('Showing 7 of 7 closed trades · viewer-local time')).toBeTruthy();
    expect(screen.getByText('Signal 1')).toBeTruthy();
  });
});
