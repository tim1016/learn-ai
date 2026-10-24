import { describe, expect, it } from 'vitest';

import { DATA_CHECKS, dataCheckOf, dataCheckRoute } from './data-checks';

describe('Data checks addresses', () => {
  it.each(DATA_CHECKS.map((check) => [check.id]))('opens the %s check at its own address, and reads it back from it', (id) => {
    const route = dataCheckRoute(id as (typeof DATA_CHECKS)[number]['id']);

    expect(route.join('/')).toBe(`/operations/data-checks/${id}`);
    expect(dataCheckOf(`${route.join('/')}?ticker=SPY#run`)).toBe(id);
  });

  it.each([
    ['/operations/data-checks'],
    ['/operations/data-checks/nothing'],
    ['/operations/lake'],
    ['/markets/SPY/data-checks/tradingview'],
  ])('names no check for %s', (url) => {
    expect(dataCheckOf(url)).toBeNull();
  });
});
