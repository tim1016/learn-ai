import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { EngineTrade } from '../engine-results.component';

@Component({
  selector: 'app-engine-trade-ledger',
  imports: [TimestampDisplayComponent],
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

  readonly winners = computed(() => this.trades().filter((trade) => trade.result === 'WIN').length);
  readonly losses = computed(() => this.trades().filter((trade) => trade.result === 'LOSS').length);

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

