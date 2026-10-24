import { ChangeDetectionStrategy, Component, effect, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import { StrategyLabRunStatsComponent } from '../../../components/strategy-lab/run-stats/strategy-lab-run-stats.component';
import { StrategyLabRunReport } from '../../../components/strategy-lab/strategy-lab-run-report.service';
import { StrategyLabStrategyViewComponent } from '../../../components/strategy-lab/strategy-lab-strategy-view/strategy-lab-strategy-view.component';
import { SIGNAL_STRATEGIES_ROUTE } from '../../signal-strategy-tabs';

/**
 * A saved backtest run that no registered strategy holds, shown by itself and
 * read-only: its report and its strategy view, with no strategy workspace around
 * them and nothing to run, sweep or approve. The strategy it stored (retired,
 * renamed, or "user_provided" for a custom LEAN source) is named as stored; no
 * registered strategy is guessed for it, and the record is unchanged.
 */
@Component({
  selector: 'app-unregistered-run-report',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, StrategyLabRunStatsComponent, StrategyLabStrategyViewComponent],
  providers: [StrategyLabRunReport],
  templateUrl: './unregistered-run-report.component.html',
  styleUrl: './unregistered-run-report.component.scss',
})
export class UnregisteredRunReportComponent {
  protected readonly report = inject(StrategyLabRunReport);
  protected readonly listRoute = SIGNAL_STRATEGIES_ROUTE;

  readonly runId = input.required<number>();
  /** The strategy name the run stored. */
  readonly storedStrategy = input.required<string>();

  constructor() {
    effect(() => this.report.activeRunId.set(this.runId()));
  }
}
