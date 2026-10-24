import { describe, expect, it } from 'vitest';

import {
  instrumentTabOf,
  instrumentWindowTitle,
  legacyInstrumentTarget,
  openableTab,
} from './instrument-address';

describe('legacyInstrumentTarget', () => {
  it('moves the ticker into the path and keeps the window, with the session under its new name', () => {
    expect(
      legacyInstrumentTarget({ ticker: 'spy', from: '2026-01-02', to: '2026-02-27', 'trading-session': 'extended' }, 'chart'),
    ).toEqual({
      commands: ['/markets', 'SPY', 'chart'],
      queryParams: { from: '2026-01-02', to: '2026-02-27', session: 'extended' },
    });
  });

  it('keeps every other key the address carried', () => {
    expect(legacyInstrumentTarget({ ticker: 'SPY', sessionId: 'abc', mode: 'bogus' }, 'chart').queryParams).toEqual({
      sessionId: 'abc',
      mode: 'bogus',
    });
  });

  it('opens the headlines under the instrument', () => {
    expect(legacyInstrumentTarget({ ticker: 'QQQ' }, 'chart', ['news']).commands).toEqual(['/markets', 'QQQ', 'chart', 'news']);
  });

  it.each([{}, { ticker: '' }, { ticker: '   ' }])('names no instrument for %j, and leads to the list, never to a default one', (query) => {
    expect(legacyInstrumentTarget(query, 'chart').commands).toEqual(['/markets']);
  });
});

describe('openableTab', () => {
  it('opens the tab it was asked for when this place routes it, and Chart otherwise', () => {
    expect(openableTab('chart')).toBe('chart');
    expect(openableTab('options')).toBe('options');
    // Returns and Data are in the strip but not routed yet.
    expect(openableTab('returns')).toBe('chart');
    expect(openableTab('nonsense')).toBe('chart');
    expect(openableTab(undefined)).toBe('chart');
  });
});

describe('an instrument address', () => {
  it.each([
    ['/markets/SPY/chart', 'chart'],
    ['/markets/SPY/chart/news?from=2026-01-02', 'chart'],
    ['/markets/SPY/returns', 'returns'],
    ['/markets/SPY/options/surface?expiry=1790000000000', 'options'],
    ['/markets/SPY', null],
    ['/markets', null],
    ['/options-lab/chain', null],
  ])('has %s on the %s tab', (url, tab) => {
    expect(instrumentTabOf(url)).toBe(tab);
  });

  it.each([
    ['/markets/spy/chart?from=2026-01-02#top', 'SPY · Chart · Markets'],
    ['/markets/SPY/chart/news', 'SPY · Headlines · Markets'],
    ['/markets/SPY/options', 'SPY · Options · Markets'],
    ['/markets/SPY/options/pricing', 'SPY · Options · Markets'],
    ['/markets/SPY', 'SPY · Markets'],
    ['/markets', null],
    ['/data-lab/export', null],
  ])('titles the window for %s as %s', (url, title) => {
    expect(instrumentWindowTitle(url)).toBe(title);
  });
});
