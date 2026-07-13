import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { EngineTrade } from '../engine-results.component';

@Component({
  selector: 'app-engine-trade-ledger',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './trade-ledger.component.html',
  styleUrl: './trade-ledger.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TradeLedgerComponent {
  readonly trades = input.required<EngineTrade[]>();
  readonly expanded = signal(false);

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
