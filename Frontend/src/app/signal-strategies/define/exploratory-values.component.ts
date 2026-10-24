import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ExploreSession } from '../explore/explore-session';
import { EXPLORE_RUN_PARAM, exploreTabRoute } from '../explore/explore-tabs';
import { ALWAYS_READY } from '../read-state';
import { valueText } from '../signal-strategy-evidence';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';

/**
 * The fourth kind of value Define distinguishes: what the Backtest workbench is
 * trying out right now. They are exploratory and in-sample, they are read from
 * the strategy's Explore scope as the workbench holds them, and nothing here
 * approves or saves them: an approved value comes only from Golden.
 */
@Component({
  selector: 'app-signal-strategy-exploratory-values',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, SignalStrategySectionComponent],
  templateUrl: './exploratory-values.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyExploratoryValuesComponent {
  private readonly session = inject(ExploreSession);

  protected readonly always = ALWAYS_READY;
  protected readonly exploring = this.session.exploratory;
  protected readonly valueText = valueText;
  protected readonly backtestRoute = computed(() => exploreTabRoute(this.session.key(), 'backtest'));
  protected readonly backtestQuery = computed(() => {
    const run = this.session.backtestRunId();
    return run === null ? undefined : { [EXPLORE_RUN_PARAM]: String(run) };
  });
}
