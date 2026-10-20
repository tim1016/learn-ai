import { describe, expect, it, vi } from 'vitest';
import { TickMarkType, type IChartApi } from 'lightweight-charts';

import { createAppChart, formatChartAxisTick } from './chart-utils';

describe('createAppChart', () => {
  it('preserves caller layout options while forcing the attribution logo off', () => {
    const container = document.createElement('div');
    const chartFactory = vi.fn(() => ({}) as IChartApi);

    createAppChart(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: true,
      },
    }, chartFactory);

    expect(chartFactory).toHaveBeenCalledWith(container, {
      width: 640,
      layout: {
        textColor: '#f0f3fa',
        attributionLogo: false,
      },
    });
  });
});

describe('formatChartAxisTick', () => {
  // 2026-03-09 14:07:08 UTC is after the US daylight-saving transition.
  const CHART_TIME_SECONDS = Date.UTC(2026, 2, 9, 14, 7, 8) / 1_000;

  it.each([
    ['local intraday ticks as HH:MM without a date or seconds', 'America/Los_Angeles', TickMarkType.Time, '07:07'],
    ['exchange-time intraday ticks as HH:MM', 'America/New_York', TickMarkType.Time, '10:07'],
    ['only the date at local day-boundary ticks', 'America/Los_Angeles', TickMarkType.DayOfMonth, 'Mar 9'],
    ['only the date at exchange-time day-boundary ticks', 'America/New_York', TickMarkType.DayOfMonth, 'Mar 9'],
  ])('renders %s', (_name, timeZone, tickType, expected) => {
    expect(formatChartAxisTick(
      CHART_TIME_SECONDS,
      timeZone,
      tickType,
    )).toBe(expected);
  });
});
