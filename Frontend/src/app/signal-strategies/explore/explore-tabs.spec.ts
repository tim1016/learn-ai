import { describe, expect, it } from 'vitest';

import { EXPLORE_TABS, exploreTabOf, exploreTabRoute } from './explore-tabs';

describe('Explore addresses', () => {
  it('builds each tool’s address under the strategy', () => {
    expect(EXPLORE_TABS.tabs.map((tab) => exploreTabRoute('sma_crossover', tab.id).join('/'))).toEqual([
      '/signal-strategies/sma_crossover/explore/backtest',
      '/signal-strategies/sma_crossover/explore/sweep',
      '/signal-strategies/sma_crossover/explore/walk-forward',
    ]);
  });

  it.each([
    ['/signal-strategies/sma_crossover/explore/backtest', 'backtest'],
    ['/signal-strategies/sma_crossover/explore/sweep?search=abc#x', 'sweep'],
    ['/signal-strategies/sma_crossover/explore/walk-forward?run=7', 'walk-forward'],
  ])('reads the tool out of %s', (url, tool) => {
    expect(exploreTabOf(url)).toBe(tool);
  });

  it.each([
    '/signal-strategies/sma_crossover/explore',
    '/signal-strategies/sma_crossover/explore/nonsense',
    '/signal-strategies/sma_crossover/define',
    '/signal-strategies/evidence/run/7',
    '/strategy-lab',
  ])('names no tool for %s', (url) => {
    expect(exploreTabOf(url)).toBeNull();
  });
});
