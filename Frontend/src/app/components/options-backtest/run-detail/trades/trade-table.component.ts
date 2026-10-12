import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { OptionContractComponent, type OptionContract } from '../../../../shared/option-contract/option-contract.component';
import { formatTimestampDisplay, TimestampDisplayComponent } from '../../../../shared/timestamp';
import type { DayRequest } from '../day-request';
import type { LegTrade } from '../run-detail.types';
import { attemptWords, contractOf } from '../words';

interface TradeRow {
  readonly key: string;
  readonly trade: LegTrade;
  readonly contract: OptionContract;
  readonly attempt: string;
  /** The name of the control that opens the trade's day: it says which trade, because one day has several. */
  readonly openLabel: string;
}

/**
 * Leg trades as a table, one row each, in the order they are given. A trade that left by a sale shows the sale; a trade
 * that was marked and not sold shows the mark in words across the sale's columns, so a mark is never read as a sale.
 * Every price, fee and profit is the server's text, shown as it came. The table scrolls sideways inside its own region.
 */
@Component({
  selector: 'app-options-backtest-trade-table',
  imports: [NgTemplateOutlet, AuthoredUsdPipe, ReceiptLabelPipe, OptionContractComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trade-table.component.html',
  styleUrl: './trade-table.component.scss',
})
export class TradeTableComponent {
  readonly trades = input.required<readonly LegTrade[]>();
  /** What the table is, for those who cannot see it: its caption. */
  readonly caption = input.required<string>();
  /** Whether each row can open its day. Off when the table is already inside a day. */
  readonly canOpenDay = input(true);
  readonly openDay = output<DayRequest>();

  /** The scrolling region's name: what the table is, and that it scrolls, because a keyboard user who finds it must know. */
  protected readonly regionLabel = computed(() => `${this.caption()} (scrolls sideways)`);

  protected readonly rows = computed<readonly TradeRow[]>(() =>
    this.trades().map((trade) => ({
      key: `${trade.session_open_ms}:${trade.trade_number}`,
      trade,
      contract: contractOf(trade.contract),
      attempt: attemptWords(trade.entry_number),
      openLabel: `Look at ${formatTimestampDisplay(trade.session_open_ms, { mode: 'date-et', granularity: 'date' })}, leg ${trade.leg_id}, ${attemptWords(trade.entry_number)}`,
    })),
  );

  protected ask(openMs: number, event: Event): void {
    this.openDay.emit({ openMs, origin: event.currentTarget instanceof HTMLElement ? event.currentTarget : null });
  }
}
