import { describe, expect, it } from 'vitest';

import { lensNavigationExtras } from './lens-url';

describe('lensNavigationExtras', () => {
  it('replaces the URL entry and merges query parameters', () => {
    const extras = lensNavigationExtras('operator');

    expect(extras.replaceUrl).toBe(true);
    expect(extras.queryParamsHandling).toBe('merge');
    expect(extras.queryParams).toEqual({ lens: 'operator' });
  });
});
