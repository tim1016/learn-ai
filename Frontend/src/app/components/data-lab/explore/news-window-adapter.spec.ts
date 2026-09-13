import { describe, expect, it } from 'vitest';

import { NEWS_MAX_HEADLINES, windowToNewsQuery } from './news-window-adapter';

describe('windowToNewsQuery (PRD §11 numeric-window adapter)', () => {
  const window = {
    // 2026-04-15T13:30:00Z → 2026-05-15T20:00:00Z, exclusive end.
    startMsUtc: Date.UTC(2026, 3, 15, 13, 30),
    endMsUtc: Date.UTC(2026, 4, 15, 20, 0),
  };

  it('submits the inclusive start as published_utc_gte in vendor format', () => {
    expect(windowToNewsQuery('SPY', window).published_utc_gte).toBe('2026-04-15T13:30:00.000Z');
  });

  it('submits the EXCLUSIVE end as published_utc_lt (never lte, never a wall-clock string)', () => {
    const query = windowToNewsQuery('SPY', window);
    expect(query.published_utc_lt).toBe('2026-05-15T20:00:00.000Z');
    expect(query.published_utc_lte).toBeUndefined();
    expect(query.published_utc_gt).toBeUndefined();
  });

  it('follows the committed ticker, newest first, bounded to five headlines', () => {
    const query = windowToNewsQuery('AAPL', window);
    expect(query.ticker).toBe('AAPL');
    expect(query.order).toBe('desc');
    expect(query.limit).toBe(NEWS_MAX_HEADLINES);
    expect(query.limit).toBe(5);
  });
});
