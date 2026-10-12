import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { RunDetail } from '../options-backtest.types';
import { RunBannerComponent } from './run-banner.component';
import { RunDayTableComponent } from './run-day-table.component';
import { RunDrawdownComponent } from './run-drawdown.component';
import { RunHeadlineComponent } from './run-headline.component';
import { RunMonthTableComponent } from './run-month-table.component';
import { RunSessionsComponent } from './run-sessions.component';
import { SlippageToleranceComponent } from './slippage-tolerance.component';

/**
 * The result of a complete run, top to bottom: the banner that says how the
 * fills were made, the headline figures, the largest falls, the sessions and
 * what did not trade, the slippage tolerance, and the day and month tables.
 * It shows the server's figures and works out none.
 */
@Component({
  selector: 'app-options-backtest-run-result',
  imports: [
    RunBannerComponent,
    RunHeadlineComponent,
    RunDrawdownComponent,
    RunSessionsComponent,
    SlippageToleranceComponent,
    RunDayTableComponent,
    RunMonthTableComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-result.component.html',
  styleUrl: './run-result.component.scss',
})
export class RunResultComponent {
  readonly run = input.required<RunDetail>();
}
