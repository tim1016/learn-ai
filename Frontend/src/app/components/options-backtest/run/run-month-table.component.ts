import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { TableModule } from 'primeng/table';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { MonthRow } from '../options-backtest.types';
import { monthLabel } from '../run-words';
import { OptionsMonthGridComponent, type MonthCell, type MonthMetric } from './options-month-grid.component';

/**
 * One row for each New York calendar month the run covered. A month is named
 * by the year and month the server counts it by; the columns of whole
 * numbers sort, the dollar columns do not.
 */
@Component({
  selector: 'app-options-backtest-run-month-table',
  imports: [TableModule, ReceiptLabelPipe, AuthoredUsdPipe, OptionsMonthGridComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-month-table.component.html',
  styleUrl: './run-tables.scss',
})
export class RunMonthTableComponent {
  readonly rows = input.required<MonthRow[]>();
  protected readonly labelled = computed(() => this.rows().map((row) => ({ ...row, label: monthLabel(row.year, row.month) })));
  protected readonly metrics: readonly MonthMetric[] = [
    { key: 'pnl', label: 'Net P&L', unit: 'usd', signed: true },
    { key: 'trades', label: 'Trades', unit: 'count', signed: false },
    { key: 'sessions', label: 'Traded sessions', unit: 'count', signed: false },
    { key: 'fees', label: 'Fees', unit: 'usd', signed: false },
  ];
  protected readonly monthCells = computed<readonly MonthCell[]>(() => this.rows().map((row) => ({ year: row.year, month: row.month, values: { pnl: row.net_profit_usd, trades: String(row.trades), sessions: String(row.sessions_traded), fees: row.fees_usd } })));
}
