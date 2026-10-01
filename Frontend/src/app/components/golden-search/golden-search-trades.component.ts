import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { CandidateTrade } from './golden-search.types';

interface TradeRow {
  readonly key: string;
  readonly trade: CandidateTrade;
  readonly rsi: string | null;
  readonly pnlText: string;
  readonly loss: boolean;
}

const PRICE = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
/** A trade's P&L to the cent: a one-share trade often moves less than a dollar. */
const SIGNED_CENTS = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });

/**
 * The decisions behind a candidate's result (#2696): each development trade
 * with its ET session date, the entry (price and the RSI the server recorded
 * at entry), the exit (its reason code when the server gave one) and the
 * P&L before fees — the server's per-trade figure is price change times
 * quantity, so calling it net would overstate it. Values are the engine's;
 * nothing is re-derived.
 */
@Component({
  selector: 'app-golden-search-trades',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-trades.component.html',
  styleUrl: './golden-search-evidence-table.scss',
})
export class GoldenSearchTradesComponent {
  readonly trades = input.required<readonly CandidateTrade[]>();
  readonly candidateLabel = input.required<string>();

  protected readonly rows = computed<TradeRow[]>(() =>
    this.trades().map((trade) => {
      const rsi = trade.indicators['rsi'];
      return {
        key: `${trade.entry_ms}|${trade.exit_ms}`,
        trade,
        rsi: typeof rsi === 'number' ? rsi.toFixed(1) : null,
        pnlText: SIGNED_CENTS.format(trade.pnl),
        loss: trade.pnl < 0,
      };
    }),
  );

  protected price(value: number): string {
    return PRICE.format(value);
  }
}
