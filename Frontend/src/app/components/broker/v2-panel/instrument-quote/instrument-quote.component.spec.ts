import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { InstrumentQuoteComponent } from './instrument-quote.component';

describe('InstrumentQuoteComponent', () => {
  it('renders the canonical ticker quote with the price source that produced it', async () => {
    const { container } = await render(InstrumentQuoteComponent, {
      inputs: {
        quote: {
          symbol: 'SPY',
          name: 'SPDR S&P 500 ETF Trust',
          exchange: 'NYSE Arca',
          price: 625.1,
          changePercent: 1.24,
        },
      },
    });

    expect(container.querySelector('app-asset-identity.asset-identity--lg')?.textContent).toContain('SPY');
    expect(screen.getByTitle(/\(SPY\) — \$625\.10/i)).toBeTruthy();
    expect(screen.getByText('+1.24%')).toBeTruthy();
    expect(screen.getByText('Polygon snapshot')).toBeTruthy();
  });

  it('keeps the source label when the price is unavailable', async () => {
    const { container } = await render(InstrumentQuoteComponent, {
      inputs: {
        quote: { symbol: 'SPY', price: null, changePercent: null },
      },
    });

    expect(container.querySelector('app-asset-identity.asset-identity--lg')?.textContent).toContain('SPY');
    expect(screen.getByText('Polygon snapshot')).toBeTruthy();
  });
});
