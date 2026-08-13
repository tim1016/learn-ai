import { render, screen } from '@testing-library/angular';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { MarketDataService } from '../../../../services/market-data.service';
import { PanelInstrumentQuoteComponent } from './panel-instrument-quote.component';

describe('PanelInstrumentQuoteComponent', () => {
  it('combines the displayed price snapshot with canonical market-pulse cues', async () => {
    const getStockSnapshot = vi.fn().mockReturnValue(of({
      success: true,
      snapshot: {
        ticker: 'NVDA',
        day: { close: 181.42 },
        min: null,
        todaysChangePercent: 1.35,
      },
      error: null,
    }));

    await render(PanelInstrumentQuoteComponent, {
      inputs: {
        symbol: 'NVDA',
        marketPulse: {
          session: 'OPEN',
          feed_state: 'LIVE',
          latest_bar_at_ms: 1_753_800_000_000,
          age_ms: 1_000,
          source: 'polygon',
          expected_cadence_ms: 60_000,
          headline: 'Market data live',
          explanation: 'The latest bar arrived on time.',
          next_step: null,
          attention_required: false,
          observed_at_ms: 1_753_800_001_000,
        },
      },
      providers: [{ provide: MarketDataService, useValue: { getStockSnapshot } }],
    });

    expect(await screen.findByText('$181.42')).toBeTruthy();
    expect(screen.getByText('+1.35%')).toBeTruthy();
    expect(screen.getByText('Open')).toBeTruthy();
    expect(screen.getByLabelText('Price data live')).toBeTruthy();
    expect(getStockSnapshot).toHaveBeenCalledWith('NVDA');
  });
});
