import { describe, expect, it } from 'vitest';

import type { ChartBar } from '../lib/broker-v2-panel.types';
import {
  selectChartIndicator,
  toActiveIndicatorChips,
  toChartIndicatorRequestBars,
  toIndicatorSeriesPlans,
} from './dual-pane-chart-indicators';

const BARS: readonly ChartBar[] = [
  {
    start_ms: 1_700_000_000_000,
    end_ms: 1_700_000_060_000,
    open: '99',
    high: '101',
    low: '98',
    close: '100',
    volume: 10,
    source: 'ibkr',
  },
];

describe('dual-pane chart indicators', () => {
  it('sends exact bar-close millisecond anchors to Python', () => {
    expect(toChartIndicatorRequestBars(BARS)).toEqual([
      { t: 1_700_000_060_000, o: 99, h: 101, l: 98, c: 100, v: 10 },
    ]);
  });

  it('maps returned bar-close values onto the matching candle render anchor', () => {
    expect(toIndicatorSeriesPlans([
      {
        id: 'sma_2',
        panel: 'main',
        type: 'line',
        color: '#FF9800',
        data: [{ t: 1_700_000_060_000, value: 100 }],
        refs: [],
      },
    ], BARS)).toEqual([
      expect.objectContaining({
        id: 'sma_2',
        points: [{ time: 1_700_000_000, value: 100 }],
      }),
    ]);
  });

  it('deduplicates the same indicator recipe', () => {
    const once = selectChartIndicator([], { name: 'sma', params: { length: 20 } });

    expect(selectChartIndicator(once, { name: 'sma', params: { length: 20 } })).toBe(once);
    expect(once[0]).toEqual(expect.objectContaining({ id: 'sma|length:20', label: 'SMA 20' }));
  });

  it('uses the Python presentation color for the active rail chip', () => {
    const selected = selectChartIndicator([], { name: 'sma', params: { length: 20 } });

    expect(toActiveIndicatorChips(selected, [{
      id: 'sma_20',
      panel: 'main',
      type: 'line',
      color: '#FF9800',
      data: [],
    }])).toEqual([{ id: 'sma|length:20', label: 'SMA 20', color: '#FF9800' }]);
  });
});
