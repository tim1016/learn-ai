import { describe, expect, it } from 'vitest';

import { deployHandoffQuery, goldenLocation, goldenStudyRoute, goldenTabRoute, proofRoute } from './golden-tabs';

describe('goldenLocation', () => {
  it.each([
    ['/signal-strategies/ema_crossover_signal/golden/studies', { tab: 'studies', studyId: null }],
    ['/signal-strategies/ema_crossover_signal/golden/studies/study-1?step=compare#x', { tab: 'studies', studyId: 'study-1' }],
    ['/signal-strategies/ema_crossover_signal/golden/versions', { tab: 'versions', studyId: null }],
    ['/signal-strategies/ema_crossover_signal/golden/configuration?run=7', { tab: 'configuration', studyId: null }],
    // The bare tab names no page yet: it redirects to its default.
    ['/signal-strategies/ema_crossover_signal/golden', { tab: null, studyId: null }],
  ])('reads the page and study out of %s', (url, expected) => {
    expect(goldenLocation(url)).toEqual(expected);
  });

  it.each([
    '/signal-strategies',
    '/signal-strategies/ema_crossover_signal/proof',
    '/signal-strategies/ema_crossover_signal/explore/sweep',
    '/signal-strategies/evidence/study/study-1',
    '/golden-search/study-1',
  ])('names no Golden page for %s', (url) => {
    expect(goldenLocation(url)).toBeNull();
  });
});

describe('Golden’s addresses', () => {
  it('builds each page under the strategy’s own address', () => {
    expect(goldenTabRoute('ema', 'versions')).toEqual(['/signal-strategies', 'ema', 'golden', 'versions']);
    expect(goldenStudyRoute('ema', 'study-1')).toEqual(['/signal-strategies', 'ema', 'golden', 'studies', 'study-1']);
    expect(proofRoute('ema')).toEqual(['/signal-strategies', 'ema', 'proof']);
  });

  it('hands Deploy the strategy and the exact version under its own query keys, and no version when there is none', () => {
    expect(deployHandoffQuery('ema', 'gq-1')).toEqual({ strategy: 'ema', golden_qualification: 'gq-1' });
    expect(deployHandoffQuery('ema', null)).toEqual({ strategy: 'ema' });
  });
});
