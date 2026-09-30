import { provideZonelessChangeDetection } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { EngineClosingBarSkip, EngineTrade } from '../engine-results.types';
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

  it('lists the decisions the closing-bar rule set aside, even when no trade closed', async () => {
    const skips: EngineClosingBarSkip[] = [
      { bar_close_ms: Date.UTC(2026, 6, 1, 20, 0, 0), intent: 'ENTER', close_price: 612.34 },
      { bar_close_ms: Date.UTC(2026, 6, 2, 20, 0, 0), intent: 'EXIT', close_price: 613.5 },
    ];
    await render(TradeLedgerComponent, {
      inputs: { trades: [], closingBarSkips: skips },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText('This run did not close any trades.')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Decided on the closing bar' })).toBeTruthy();
    expect(screen.getByText('Entry skipped')).toBeTruthy();
    expect(screen.getByText('Exit moved to the next session')).toBeTruthy();
    expect(screen.getByText('612.34')).toBeTruthy();
  });

  it('shows no closing-bar section when the run set nothing aside', async () => {
    await render(TradeLedgerComponent, {
      inputs: { trades: [trade(2)] },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.queryByRole('heading', { name: 'Decided on the closing bar' })).toBeNull();
  });
});
