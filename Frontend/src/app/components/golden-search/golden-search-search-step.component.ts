import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';

import { pairMapView } from './golden-search-compare';
import { GoldenSearchPairMapComponent } from './golden-search-pair-map.component';
import { GoldenSearchProcedureComponent } from './golden-search-procedure.component';
import type { StrategyCapability, StudyDetail } from './golden-search.types';

/**
 * The Search step (#2696): the all-period procedure fitted on the whole
 * development interval, its pair landscapes beside why a pair grid is the
 * check on a one-knob path, the recent-window procedure when the plan asked
 * for one, and the study's engine-run accounting (used, cached, skipped as
 * invalid). Everything here is development evidence — it chose the settings.
 */
@Component({
  selector: 'app-golden-search-search-step',
  imports: [DecimalPipe, GoldenSearchPairMapComponent, GoldenSearchProcedureComponent],
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
  protected readonly pairMaps = computed(() => this.study().results.search?.pair_maps ?? []);
  /** Each landscape's valid and invalid cells, counted from the server's cells. */
  protected readonly audits = computed(() => {
    const center = this.study().results.search?.winner ?? null;
    return this.pairMaps().map((map) => pairMapView(map, this.capability(), center));
  });
}
