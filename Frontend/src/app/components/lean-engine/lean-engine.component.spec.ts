import { mapStudyTradeToEngineTrade, StudyTradeApiItem } from './lean-engine.component';

describe('mapStudyTradeToEngineTrade', () => {
  function makeItem(overrides: Partial<StudyTradeApiItem> = {}): StudyTradeApiItem {
    return {
      entryTimestamp: '2026-04-01T13:30:00Z',
      exitTimestamp: '2026-04-01T15:00:00Z',
      entryPrice: 500,
      exitPrice: 505,
      pnL: 5,
      signalReason: 'crossover',
      ...overrides,
    };
  }

  // Regression: prior to the fix, History → reload populated pnl_pct as 0
  // because the .NET BacktestTrade entity does not persist a percent column.
  // pnl_pct must be derived as pnl / entryPrice, matching the Python engine.
  it('derives pnl_pct from pnL / entryPrice when entry price is positive', () => {
    const trade = mapStudyTradeToEngineTrade(makeItem({ pnL: 5, entryPrice: 500 }), 0);
    expect(trade.pnl_pct).toBeCloseTo(0.01, 10);
    expect(trade.pnl_pts).toBe(5);
  });

  it('preserves the sign of pnl_pct for losing trades', () => {
    const trade = mapStudyTradeToEngineTrade(makeItem({ pnL: -1.705, entryPrice: 563.4 }), 0);
    expect(trade.pnl_pct).toBeCloseTo(-0.00302627, 8);
    expect(trade.result).toBe('LOSS');
  });

  it('falls back to 0 when entry price is zero or negative to avoid NaN/Infinity', () => {
    expect(mapStudyTradeToEngineTrade(makeItem({ entryPrice: 0, pnL: 1 }), 0).pnl_pct).toBe(0);
    expect(mapStudyTradeToEngineTrade(makeItem({ entryPrice: -5, pnL: 1 }), 0).pnl_pct).toBe(0);
  });

  it('passes through trade fields and assigns a 1-based trade_number', () => {
    const trade = mapStudyTradeToEngineTrade(
      makeItem({ pnL: 2.5, entryPrice: 100, signalReason: 'rsi_oversold' }),
      4,
    );
    expect(trade.trade_number).toBe(5);
    expect(trade.entry_time).toBe('2026-04-01T13:30:00Z');
    expect(trade.exit_time).toBe('2026-04-01T15:00:00Z');
    expect(trade.entry_price).toBe(100);
    expect(trade.exit_price).toBe(505);
    expect(trade.signal_reason).toBe('rsi_oversold');
    expect(trade.result).toBe('WIN');
    expect(trade.indicators).toEqual({});
  });

  it('handles missing signalReason as empty string', () => {
    const trade = mapStudyTradeToEngineTrade(makeItem({ signalReason: null }), 0);
    expect(trade.signal_reason).toBe('');
  });
});
