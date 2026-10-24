import { describe, expect, it } from 'vitest';

import { NEWS_MAX_HEADLINES, windowToNewsQuery } from './news-window-adapter';

describe('windowToNewsQuery (PRD §11 numeric-window adapter)', () => {
  const window = {
    // 2026-04-15T13:30:00Z → 2026-05-15T20:00:00Z, exclusive end.
    startMsUtc: Date.UTC(2026, 3, 15, 13, 30),
    endMsUtc: Date.UTC(2026, 4, 15, 20, 0),
  };

  it('maps the window to a vendor news query: inclusive start, exclusive end, committed ticker, newest first, bounded', () => {
    const spy = windowToNewsQuery('SPY', window);
    expect.soft(spy.published_utc_gte, 'submits the inclusive start as published_utc_gte in vendor format').toBe('2026-04-15T13:30:00.000Z');
    expect.soft(spy.published_utc_lt, 'submits the EXCLUSIVE end as published_utc_lt (never a wall-clock string)').toBe('2026-05-15T20:00:00.000Z');
    expect.soft(spy.published_utc_lte, 'submits the EXCLUSIVE end as published_utc_lt (never lte)').toBeUndefined();
    expect.soft(spy.published_utc_gt, 'submits the EXCLUSIVE end as published_utc_lt (never gt)').toBeUndefined();

    const aapl = windowToNewsQuery('AAPL', window);
    expect.soft(aapl.ticker, 'follows the committed ticker').toBe('AAPL');
    expect.soft(aapl.order, 'orders newest first').toBe('desc');
    expect.soft(aapl.limit, 'bounds to NEWS_MAX_HEADLINES').toBe(NEWS_MAX_HEADLINES);
    expect.soft(aapl.limit, 'bounds to five headlines').toBe(5);
  });
});
