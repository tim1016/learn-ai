import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { EngineClosingBarSkip, EngineTrade } from '../engine-results.types';

type ClosingBarIntent = EngineClosingBarSkip['intent'];

/** Closed copy for what the closing-bar rule (#2607) did to each decision it set aside. */
const CLOSING_BAR_OUTCOME: Record<ClosingBarIntent, string> = {
  ENTER: 'Entry skipped',
  EXIT: 'Exit stayed due',
};
const CLOSING_BAR_PREAMBLE =
  'The bar that ends at the session close is decided only after the market closes, so live cannot trade it '
  + 'and this backtest did not.';
/** What happened to each kind of set-aside decision, stated only for the kinds a run has. */
const CLOSING_BAR_CONSEQUENCE: Record<ClosingBarIntent, string> = {
  ENTER: 'An entry decided there produced no trade.',
  EXIT: 'An exit decided there stayed due, and the program decided it again from the next session.',
};
const CLOSING_BAR_INTENTS: readonly ClosingBarIntent[] = ['ENTER', 'EXIT'];

@Component({
  selector: 'app-engine-trade-ledger',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './trade-ledger.component.html',
  styleUrl: './trade-ledger.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TradeLedgerComponent {
  readonly trades = input.required<EngineTrade[]>();
  readonly totalTradeCount = input<number | null>(null);
  readonly tradesTruncated = input(false);
  readonly closingBarSkips = input<EngineClosingBarSkip[]>([]);
  readonly expanded = signal(false);

  readonly closingBarNote = computed(() => {
    const present = new Set(this.closingBarSkips().map((skip) => skip.intent));
    const consequences = CLOSING_BAR_INTENTS
      .filter((intent) => present.has(intent))
      .map((intent) => CLOSING_BAR_CONSEQUENCE[intent]);
    return [CLOSING_BAR_PREAMBLE, ...consequences].join(' ');
  });

  readonly isTruncated = computed(() =>
    this.tradesTruncated()
      || (this.totalTradeCount() ?? this.trades().length) > this.trades().length,
  );

  readonly visibleTrades = computed(() => {
    const newestFirst = [...this.trades()].reverse();
    return this.expanded() ? newestFirst : newestFirst.slice(0, 6);
  });

  private readonly tradeOutcomeTally = computed(() => {
    let winners = 0;
    let losses = 0;
    for (const trade of this.trades()) {
      if (trade.result === 'WIN') winners += 1;
      if (trade.result === 'LOSS') losses += 1;
    }
    return { winners, losses };
  });

  readonly winners = computed(() => this.tradeOutcomeTally().winners);
  readonly losses = computed(() => this.tradeOutcomeTally().losses);

  closingBarOutcome(skip: EngineClosingBarSkip): string {
    return CLOSING_BAR_OUTCOME[skip.intent];
  }

  toggleExpanded(): void {
    this.expanded.update((value) => !value);
  }

  formatNumber(value: number, places = 2): string {
    return value.toFixed(places);
  }

  formatPercent(value: number): string {
    const sign = value > 0 ? '+' : '';
    return `${sign}${(value * 100).toFixed(2)}%`;
  }
}
