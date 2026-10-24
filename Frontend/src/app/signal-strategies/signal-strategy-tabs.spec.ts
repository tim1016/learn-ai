import { describe, expect, it } from 'vitest';

import { signalStrategyLocation, signalStrategyWindowTitle } from './signal-strategy-tabs';

describe('signalStrategyLocation', () => {
  it.each([
    ['/signal-strategies/ema_crossover_signal/overview', { strategyKey: 'ema_crossover_signal', tab: 'overview' }],
    ['/signal-strategies/ema_crossover_signal/define?x=1#y', { strategyKey: 'ema_crossover_signal', tab: 'define' }],
    // A page deeper inside a tab is still that tab.
    ['/signal-strategies/sma_crossover/explore/backtest', { strategyKey: 'sma_crossover', tab: 'explore' }],
    ['/signal-strategies/sma_crossover/nonsense', { strategyKey: 'sma_crossover', tab: null }],
  ])('reads the strategy and tab out of %s', (url, expected) => {
    expect(signalStrategyLocation(url)).toEqual(expected);
  });

  it.each([
    '/signal-strategies',
    '/signal-strategies/recency',
    '/signal-strategies/evidence/run/7',
    '/strategy-lab',
    '/',
  ])('names no strategy for %s', (url) => {
    expect(signalStrategyLocation(url)).toBeNull();
  });

  it('titles a strategy’s page by the entity, its tab, then the group', () => {
    expect(signalStrategyWindowTitle({ strategyKey: 'sma_crossover', tab: 'define' })).toBe(
      'sma_crossover · Define · Signal strategies',
    );
  });
});
