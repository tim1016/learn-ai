import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { TableModule } from 'primeng/table';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { dayRequestFrom, lookAtLabel, type DayRequest } from '../run-detail/day-request';
import type { DayRow } from '../options-backtest.types';

/**
 * One row for each session of the run: its date (the session's open, in New
 * York dates), how it came out, its trades, its profit and its fees. The
 * columns that are whole numbers or dates sort; a dollar column does not,
 * because ordering the exact strings would mean reading them as numbers. A row can ask for its day to be looked at closely.
 */
@Component({
  selector: 'app-options-backtest-run-day-table',
  imports: [TableModule, TimestampDisplayComponent, ReceiptLabelPipe, AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-day-table.component.html',
  styleUrl: './run-tables.scss',
})
export class RunDayTableComponent {
  readonly rows = input.required<DayRow[]>();
  protected readonly pageSize = 25;
  readonly openDay = output<DayRequest>();

  /** The name of the control that opens a day: it says which day, because the table holds many. */
  protected readonly openLabel = lookAtLabel;

  protected ask(openMs: number, event: Event): void {
    this.openDay.emit(dayRequestFrom(openMs, event));
  }
}
