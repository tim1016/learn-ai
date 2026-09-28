import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import {
  TickerQuoteComponent,
  type TickerQuoteView,
} from '../../../../shared/ticker-quote/ticker-quote.component';

export interface InstrumentQuoteView {
  readonly symbol: string;
  readonly name?: string | null;
  readonly exchange?: string | null;
  readonly logoSlug?: string | null;
  readonly price: number | null;
  readonly changePercent: number | null;
  readonly currencySymbol?: string;
}

/**
 * Dense, reusable instrument context for a bot detail chart or operator banner.
 *
 * The displayed price and its source label travel together: every caller names
 * where its price came from, so the label can never outlive a change of source.
 * Bot-feed health is presented elsewhere because it does not establish the
 * freshness of this price.
 */
@Component({
  selector: 'app-instrument-quote',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, TickerQuoteComponent],
  templateUrl: './instrument-quote.component.html',
  styleUrl: './instrument-quote.component.scss',
})
export class InstrumentQuoteComponent {
  readonly quote = input.required<InstrumentQuoteView>();
  readonly sourceLabel = input.required<string>();

  protected readonly tickerQuote = computed<TickerQuoteView | null>(() => {
    const quote = this.quote();
    if (quote.price === null) return null;
    return {
      ticker: quote.symbol,
      name: quote.name,
      exchange: quote.exchange,
      logoSlug: quote.logoSlug,
      price: quote.price,
      changePercent: quote.changePercent,
      currencySymbol: quote.currencySymbol,
    };
  });
}
