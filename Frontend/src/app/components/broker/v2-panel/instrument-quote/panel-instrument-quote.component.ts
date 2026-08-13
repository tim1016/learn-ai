import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import type { TickerQuoteView } from '../../../../shared/ticker-quote/ticker-quote.component';
import {
  InstrumentQuoteComponent,
  type InstrumentQuoteView,
} from './instrument-quote.component';

/**
 * Adapts the shell-owned displayed price for the shared quote component. The
 * Polygon source label stays attached to the price, so a separate bot-feed
 * health signal cannot be mistaken for price freshness.
 */
@Component({
  selector: 'app-panel-instrument-quote',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [InstrumentQuoteComponent],
  template: `
    <app-instrument-quote [quote]="quote()" />
  `,
})
export class PanelInstrumentQuoteComponent {
  readonly symbol = input.required<string>();
  readonly tickerQuote = input<TickerQuoteView | null>(null);

  protected readonly quote = computed<InstrumentQuoteView>(() => {
    const snapshot = this.tickerQuote();
    return {
      symbol: snapshot?.ticker ?? this.symbol(),
      price: snapshot?.price ?? null,
      changePercent: snapshot?.changePercent ?? null,
    };
  });
}
