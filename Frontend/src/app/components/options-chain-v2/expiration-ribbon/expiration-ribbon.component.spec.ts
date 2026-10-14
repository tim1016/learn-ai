import { render } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { optionExpiryAnchorMs } from '../../../shared/option-contract/option-expiry-date';
import { ExpirationRibbonComponent } from './expiration-ribbon.component';

function monthLabels(container: HTMLElement): string[] {
  return [...container.querySelectorAll('span.uppercase')].map(el =>
    (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
  );
}

describe('ExpirationRibbonComponent year marker', () => {
  afterEach(() => vi.useRealTimers());

  // The marker compares each group's New York year with "this year". The two
  // instants sit either side of New York's new year, so whatever zone the
  // viewer is in, one of them splits the viewer's year from New York's.
  it.each([
    // 23:30 ET on 2026-12-31: New York is still in 2026.
    ['before New York new year', Date.UTC(2027, 0, 1, 4, 30), ['Dec', "Jan '27"]],
    // 00:30 ET on 2027-01-01: New York is already in 2027.
    ['after New York new year', Date.UTC(2027, 0, 1, 5, 30), ["Dec '26", 'Jan']],
  ])('reads this year in New York, %s', async (_label, nowMs, expected) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(nowMs);

    const { container } = await render(ExpirationRibbonComponent, {
      inputs: {
        expirations: [optionExpiryAnchorMs('2026-12-31'), optionExpiryAnchorMs('2027-01-15')],
      },
    });

    expect(monthLabels(container)).toEqual(expected);
  });
});
