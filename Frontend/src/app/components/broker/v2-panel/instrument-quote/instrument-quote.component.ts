import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';

import type { MarketPulseView } from '../lib/broker-v2-panel.types';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';

export interface InstrumentQuoteView {
  readonly symbol: string;
  readonly name?: string | null;
  readonly exchange?: string | null;
  readonly logoSlug?: string | null;
  readonly price: number | null;
  readonly changePercent: number | null;
  readonly currencySymbol?: string;
}

type ExchangeSession = MarketPulseView['session'];
type PriceFeedState = MarketPulseView['feed_state'];

const SESSION_LABELS: Record<ExchangeSession, string> = {
  PRE_MARKET: 'Pre-market',
  OPEN: 'Open',
  AFTER_HOURS: 'After-hours',
  CLOSED: 'Closed',
  UNKNOWN: 'Unknown',
};

const FEED_LABELS: Record<PriceFeedState, string> = {
  LIVE: 'Price data live',
  IDLE: 'Price data idle',
  STALE: 'Price data stale',
  MISSING: 'Price data unavailable',
};

/**
 * Dense, reusable instrument context for a bot detail chart or operator banner.
 *
 * The parent supplies the displayed-price snapshot and the canonical market
 * pulse independently, so the freshness dot can always describe that price's
 * actual data source rather than guessing from wall-clock time.
 */
@Component({
  selector: 'app-instrument-quote',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, DecimalPipe],
  templateUrl: './instrument-quote.component.html',
  styleUrl: './instrument-quote.component.scss',
  host: {
    '[attr.data-session]': 'session().toLowerCase()',
    '[attr.data-feed-state]': 'feedState().toLowerCase()',
  },
})
export class InstrumentQuoteComponent {
  readonly quote = input.required<InstrumentQuoteView>();
  readonly session = input.required<ExchangeSession>();
  readonly feedState = input.required<PriceFeedState>();

  protected readonly sessionLabel = computed(() => SESSION_LABELS[this.session()]);
  protected readonly freshnessLabel = computed(() => FEED_LABELS[this.feedState()]);
  protected readonly currencySymbol = computed(() => this.quote().currencySymbol ?? '$');
  protected readonly changePrefix = computed(() =>
    (this.quote().changePercent ?? 0) > 0 ? '+' : '',
  );
  protected readonly changeTone = computed(() => {
    const change = this.quote().changePercent;
    if (change === null) return 'unavailable';
    if (change > 0) return 'positive';
    if (change < 0) return 'negative';
    return 'flat';
  });
}
