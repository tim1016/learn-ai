import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { DecisionExplanationView } from '../lib/broker-v2-panel.types';
import { orderedChecks, valuesLine } from './strategy-view-model';

/**
 * One decision bar's checks and values, exactly as the backend worded them
 * (#2639): label, what the bar showed, what the rule needs, pass or fail —
 * then the bot's own recorded values. The candle popover and an expanded
 * decision row both render this one table.
 *
 * Rules that could act on the bar come first; a rule that did not apply
 * (an exit rule while flat) stays listed, muted, because a gate may shade by it.
 */
@Component({
  selector: 'app-strategy-checks',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './strategy-checks.component.html',
  styleUrl: './strategy-checks.component.scss',
})
export class StrategyChecksComponent {
  readonly explanation = input.required<DecisionExplanationView>();

  protected readonly checks = computed(() => orderedChecks(this.explanation().checks));
  protected readonly values = computed(() => valuesLine(this.explanation().values));
}
