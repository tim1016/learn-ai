import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { PanelInstrumentQuoteComponent } from './panel-instrument-quote.component';

describe('PanelInstrumentQuoteComponent', () => {
  it('labels the displayed Polygon snapshot without borrowing bot-feed freshness', async () => {
    await render(PanelInstrumentQuoteComponent, {
      inputs: {
        symbol: 'NVDA',
        tickerQuote: {
          ticker: 'NVDA',
          price: 181.42,
          changePercent: 1.35,
        },
      },
    });

    expect(await screen.findByText('$181.42')).toBeTruthy();
    expect(screen.getByText('+1.35%')).toBeTruthy();
    expect(screen.getByText('Polygon snapshot')).toBeTruthy();
    expect(screen.queryByText(/Price data live/i)).toBeNull();
  });
});
