import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { InstrumentQuoteComponent } from './instrument-quote.component';

describe('InstrumentQuoteComponent', () => {
  it('renders a canonical asset identity, price, signed change, session, and live freshness', async () => {
    const { container } = await render(InstrumentQuoteComponent, {
      inputs: {
        quote: {
          symbol: 'SPY',
          name: 'SPDR S&P 500 ETF Trust',
          exchange: 'NYSE Arca',
          price: 625.1,
          changePercent: 1.24,
        },
        session: 'OPEN',
        feedState: 'LIVE',
      },
    });

    expect(container.querySelector('app-asset-identity.asset-identity--lg')?.textContent).toContain('SPY');
    expect(screen.getByLabelText('Latest price').textContent).toContain('$625.10');
    expect(screen.getByText('+1.24%')).toBeTruthy();
    expect(screen.getByText('Open')).toBeTruthy();
    expect(screen.getByLabelText('Price data live')).toBeTruthy();
  });

  it.each([
    ['PRE_MARKET', 'Pre-market'],
    ['AFTER_HOURS', 'After-hours'],
    ['CLOSED', 'Closed'],
    ['UNKNOWN', 'Unknown'],
  ] as const)('uses the server-authored %s session as %s', async (session, label) => {
    await render(InstrumentQuoteComponent, {
      inputs: {
        quote: { symbol: 'SPY', price: null, changePercent: null },
        session,
        feedState: 'STALE',
      },
    });

    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.getByLabelText('Price data stale')).toBeTruthy();
    expect(screen.getByLabelText('Latest price').textContent).toContain('—');
  });
});
