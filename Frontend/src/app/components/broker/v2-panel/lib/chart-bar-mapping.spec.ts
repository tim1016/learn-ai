import { describe, expect, it } from 'vitest';

import type { ChartBar } from './broker-v2-panel.types';
import { toCandle } from './chart-bar-mapping';

function bar(overrides: Partial<ChartBar> = {}): ChartBar {
  return {
    start_ms: 1_700_000_000_000,
    end_ms: 1_700_000_060_000,
    open: '100.25',
    high: '101.50',
    low: '99.75',
    close: '100.90',
    volume: 1_234,
    source: 'ibkr',
    ...overrides,
  };
}

describe('toCandle', () => {
  it('converts the OHLC decimal strings to numbers', () => {
    expect(toCandle(bar())).toEqual({
      time: 1_700_000_000,
      open: 100.25,
      high: 101.5,
      low: 99.75,
      close: 100.9,
    });
  });

  it('floors start_ms to whole seconds for the chart time axis', () => {
    expect(toCandle(bar({ start_ms: 1_700_000_000_999 })).time).toBe(1_700_000_000);
  });
});
