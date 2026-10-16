import { ChangeDetectionStrategy, Component, input, signal } from '@angular/core';

import type { RunDetail } from '../options-backtest.types';
import type { DayRequest } from '../run-detail/day-request';
import { SessionViewComponent } from '../run-detail/session/session-view.component';
import { RunTradesComponent } from '../run-detail/trades/run-trades.component';
import { CapitalEvidenceComponent } from './capital-evidence.component';
import { CashLadderComponent } from './cash-ladder.component';
import { RunBannerComponent } from './run-banner.component';
import { RunDayTableComponent } from './run-day-table.component';
import { RunDailyChartComponent } from './run-daily-chart.component';
import { RunDrawdownComponent } from './run-drawdown.component';
import { RunHeadlineComponent } from './run-headline.component';
import { RunMonthTableComponent } from './run-month-table.component';
import { RunSessionsComponent } from './run-sessions.component';
import { SlippageToleranceComponent } from './slippage-tolerance.component';

/**
 * The result of a complete run, top to bottom: the banner that says how the
 * fills were made, the headline figures, the largest falls, the capital
 * evidence, the sessions and what did not trade, the slippage tolerance, the
 * cash ladder, and the day and month tables. It shows the server's figures and
 * works out none.
 */
@Component({
  selector: 'app-options-backtest-run-result',
  imports: [
    RunBannerComponent,
    RunHeadlineComponent,
    RunDrawdownComponent,
    CapitalEvidenceComponent,
    RunSessionsComponent,
    SlippageToleranceComponent,
    CashLadderComponent,
    RunDayTableComponent,
    RunDailyChartComponent,
    RunMonthTableComponent,
    RunTradesComponent,
    SessionViewComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-result.component.html',
  styleUrl: './run-result.component.scss',
})
export class RunResultComponent {
  readonly run = input.required<RunDetail>();
  /** The day the owner last asked to look at, from a trade or from the table of days. */
  protected readonly day = signal<DayRequest | null>(null);
}
