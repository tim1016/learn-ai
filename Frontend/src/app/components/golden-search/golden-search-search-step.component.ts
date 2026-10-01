import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';

import { GoldenSearchProcedureComponent } from './golden-search-procedure.component';
import type { StrategyCapability, StudyDetail } from './golden-search.types';

/**
 * The Search step (#2696): the all-period procedure fitted on the whole
 * development interval, the recent-window procedure when the plan asked for
 * one, and the study's engine-run accounting (used, cached, skipped as
 * invalid). Everything here is development evidence — it chose the settings.
 */
@Component({
  selector: 'app-golden-search-search-step',
  imports: [DecimalPipe, GoldenSearchProcedureComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-search-step.component.html',
  styleUrl: './golden-search-search-step.component.scss',
})
export class GoldenSearchSearchStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  protected readonly pending = computed(() => {
    const study = this.study();
    if (study.state === 'locked') return 'The search has not started. Start it once the frozen plan is what you want to test.';
    if (study.state === 'search_running') return 'The search is running; its path appears here when it finishes.';
    return 'No search result is recorded for this study.';
  });
  protected readonly recentDescription = computed(
    () => `Fit with the same rules on only the last ${this.study().protocol.training_months} months of development. Also in-sample.`,
  );
}
