import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { MarketPulseView } from '../lib/broker-v2-panel.types';
import { MarketDataService } from '../../../../services/market-data.service';
import {
  InstrumentQuoteComponent,
  type InstrumentQuoteView,
} from './instrument-quote.component';

/**
 * Resolves the displayed price once and delegates its rendering to the shared
 * quote component. Both bot-detail lenses consume this bridge, ensuring they
 * use the same snapshot and market-pulse semantics.
 */
@Component({
  selector: 'app-panel-instrument-quote',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [InstrumentQuoteComponent],
  template: `
    <app-instrument-quote
      [quote]="quote()"
      [session]="marketPulse().session"
      [feedState]="marketPulse().feed_state"
    />
  `,
})
export class PanelInstrumentQuoteComponent {
  private readonly marketData = inject(MarketDataService);

  readonly symbol = input.required<string>();
  readonly marketPulse = input.required<MarketPulseView>();

  private readonly marketSnapshot = resource({
    params: () => this.symbol(),
    loader: ({ params: symbol }) => firstValueFrom(this.marketData.getStockSnapshot(symbol)),
  });

  protected readonly quote = computed<InstrumentQuoteView>(() => {
    const snapshot = this.marketSnapshot.hasValue()
      ? this.marketSnapshot.value().snapshot
      : null;
    return {
      symbol: snapshot?.ticker ?? this.symbol(),
      price: snapshot?.day?.close ?? snapshot?.min?.close ?? null,
      changePercent: snapshot?.todaysChangePercent ?? null,
    };
  });
}
