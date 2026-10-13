import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { AuthoredUsdPipe } from '../../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { OptionContractComponent, type OptionContract } from '../../../../shared/option-contract/option-contract.component';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { dayRequestFrom, lookAtLabel, type DayRequest } from '../day-request';
import type { LegTrade } from '../run-detail.types';
import { attemptWords, contractOf, isSoldLegTrade, markWords, SOLD_FILL_WORDS } from '../words';

interface TradeRow {
  readonly key: string;
  readonly trade: LegTrade;
  readonly contract: OptionContract;
  readonly attempt: string;
  /** The name of the control that opens the trade's day: it says which trade, because one day has several. */
  readonly openLabel: string;
  /** How a sold leg opened, in the words of its fills; null for a bought leg, whose entry is the table's own word. */
  readonly opening: string | null;
  /** How a sold leg that was closed left, in the words of its fills; null for a bought leg and for a leg that was marked. */
  readonly closing: string | null;
  /** What a mark of this trade is, in a few words: a bought leg's is not sold and a sold leg's is owed. */
  readonly markedAs: string;
}

/**
 * Leg trades as a table, one row each, in the order they are given. A trade that left by a sale shows the sale; a trade
 * that was marked and not sold shows the mark in words across the sale's columns, so a mark is never read as a sale.
 * A sold leg says so (#3109): it was sold to open and bought back to close, and a mark of it is owed. A bought leg's row
 * has no such words and reads as it always did.
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
      openLabel: `${lookAtLabel(trade.session_open_ms)}, leg ${trade.leg_id}, ${attemptWords(trade.entry_number)}`,
      opening: isSoldLegTrade(trade) ? SOLD_FILL_WORDS.entry.name : null,
      closing: isSoldLegTrade(trade) && trade.sale != null ? SOLD_FILL_WORDS.exit.name : null,
      markedAs: markWords({ owed: isSoldLegTrade(trade) }).inline,
    })),
  );

  protected ask(openMs: number, event: Event): void {
    this.openDay.emit(dayRequestFrom(openMs, event));
  }
}
