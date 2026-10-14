import { render, screen, waitFor } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { of } from 'rxjs';

import { MarketDataService } from '../../../services/market-data.service';
import { OptionsLabChainComponent } from './options-lab-chain.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { optionExpiryAnchorMs } from '../../../shared/option-contract/option-expiry-date';
import type { StockTickerSnapshot } from '../../../graphql/types';

/**
 * Renders the page against a stubbed market-data service whose chain
 * `underlying` carries a break-even delta (the vendor fills `change` and
 * `changePercent` from it) that differs from the stock snapshot's move.
 */
async function renderChain(opts: {
  expirations: number[];
  snapshot: StockTickerSnapshot | null;
}) {
  const breakEvenDelta = 9.87;
  return render(OptionsLabChainComponent, {
    providers: [
      ...fakePickerWorld().providers,
      {
        provide: MarketDataService,
        useValue: {
          getOptionsExpirations: () => of(opts.expirations),
          getStockSnapshot: () => of({
            success: opts.snapshot !== null,
            snapshot: opts.snapshot,
            error: null,
          }),
          getOptionsChainSnapshot: () => of({
            success: true,
            underlying: {
              ticker: 'SPY', price: 700, change: breakEvenDelta, changePercent: breakEvenDelta,
            },
            contracts: [],
            count: 0,
            riskFreeRate: null,
            dividendYield: null,
            rateSource: null,
            dividendSource: null,
            error: null,
          }),
        },
      },
    ],
  });
}

function stockSnapshot(overrides: Partial<StockTickerSnapshot>): StockTickerSnapshot {
  return {
    ticker: 'SPY',
    day: { open: 1, high: 1, low: 1, close: 700, volume: 1, vwap: 1 },
    prevDay: null,
    min: null,
    todaysChange: null,
    todaysChangePercent: null,
    updated: null,
    ...overrides,
  };
}

describe('OptionsLabChainComponent render', () => {
  it('renders the toolbar underlying with the shared asset identity', async () => {
    const { container } = await render(OptionsLabChainComponent, {
      providers: [
        ...fakePickerWorld().providers,
        {
        provide: MarketDataService,
        useValue: {
          getOptionsExpirations: () => of([]),
          getStockSnapshot: () => of({ success: false, snapshot: null }),
        },
      }],
    });

    // Scoped to the pill: the toolbar's symbol picker also renders the
    // current ticker through an asset identity.
    const identity = container.querySelector('.ticker-pill app-asset-identity');
    expect(identity).not.toBeNull();
    expect(identity?.getAttribute('title')).toBe('SPY');
    expect(identity?.textContent).toContain('SPY');
  });
});

describe('OptionsLabChainComponent header and expiry summary', () => {
  afterEach(() => vi.useRealTimers());

  it('shows the stock snapshot move, not the chain break-even delta', async () => {
    const { container } = await renderChain({
      expirations: [optionExpiryAnchorMs('2099-01-15')],
      snapshot: stockSnapshot({ todaysChange: 2.5, todaysChangePercent: 0.35 }),
    });

    const change = container.querySelector('.spot-change');
    await waitFor(() => expect(change?.textContent).toContain('+2.50'));
    expect(change?.textContent).toContain('(+0.35%)');
    expect(change?.textContent).not.toContain('9.87');
  });

  it('shows a dash for the move when the stock snapshot is unavailable', async () => {
    const { container } = await renderChain({
      expirations: [optionExpiryAnchorMs('2099-01-15')],
      snapshot: null,
    });

    // The chain still loads (its summary renders); only the move is withheld.
    await screen.findByText(/DTE/);
    const change = container.querySelector('.spot-change');
    expect(change?.textContent?.trim()).toBe('—');
    expect(container.textContent).not.toContain('9.87');
  });

  // 11:00 ET on Wed 2026-10-14. The expiry anchor is 16:00 ET on its date.
  it.each([
    ['2026-10-14', '0 DTE'],
    ['2026-10-15', '1 DTE'],
    ['2026-10-16', '2 DTE'],
  ])('counts calendar days to the expiry %s as %s', async (expiryDate, expected) => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(Date.UTC(2026, 9, 14, 15, 0));

    await renderChain({
      expirations: [optionExpiryAnchorMs(expiryDate)],
      snapshot: stockSnapshot({ todaysChange: 0, todaysChangePercent: 0 }),
    });

    expect(await screen.findByText(expected)).toBeTruthy();
  });
});
