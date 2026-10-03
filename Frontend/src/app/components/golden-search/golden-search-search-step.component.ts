import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { DecimalPipe } from '@angular/common';

import { extractServerMessage } from '../broker/operation-error';
import { reloadOnProgress } from './charts/golden-search-reload';
import { pairAudit } from './golden-search-compare';
import { GoldenSearchProcedureChartsComponent } from './golden-search-procedure-charts.component';
import { GoldenSearchProcedureComponent } from './golden-search-procedure.component';
import { GoldenSearchService } from './golden-search.service';
import { isLive, type SearchCharts, type StrategyCapability, type StudyDetail } from './golden-search.types';

/**
 * The Search step (#2696): the all-period procedure fitted on the whole
 * development interval, the recent-window procedure when the plan asked for
 * one, and the study's engine-run accounting. Their charts (#2821) — the
 * path Zoom took, each knob's move and profile, every point scored against
 * the rules, and the pair landscape — come from their own read, read again
 * when the study's revision moves. Everything here is development evidence:
 * it chose the settings.
 */
@Component({
  selector: 'app-golden-search-search-step',
  imports: [DecimalPipe, GoldenSearchProcedureChartsComponent, GoldenSearchProcedureComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-search-step.component.html',
  styleUrl: './golden-search-search-step.component.scss',
})
export class GoldenSearchSearchStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  private readonly service = inject(GoldenSearchService);

  private readonly request = computed(() => (this.study().results.search === null ? undefined : { studyId: this.study().id }), {
    equal: (a, b) => a?.studyId === b?.studyId,
  });
  private readonly charts = resource({
    params: () => this.request(),
    loader: ({ params }) => this.service.searchCharts(params.studyId),
  });
  private readonly revision = computed(() => this.study().revision);
  private readonly chartData = computed<SearchCharts | null>(() => (this.charts.hasValue() ? this.charts.value() : null));
  protected readonly chartsError = computed(() => {
    const error = this.charts.error();
    return error === undefined ? null : extractServerMessage(error, 'The search charts could not be loaded.');
  });
  /** Each recorded procedure's charts, by key. */
  protected readonly procedures = computed(() => new Map((this.chartData()?.procedures ?? []).map((procedure) => [procedure.key, procedure] as const)));

  protected readonly pending = computed(() => {
    const study = this.study();
    if (study.state === 'locked') return 'The search has not started. Start it once the frozen plan is what you want to test.';
    if (study.state === 'search_running') {
      return isLive(study.presented_status)
        ? 'The search is running; its path appears here when it finishes.'
        : 'The search stopped before it finished; its path appears here once it does.';
    }
    return 'No search result is recorded for this study.';
  });
  protected readonly recentDescription = computed(
    () => `Fit with the same rules on only the last ${this.study().protocol.training_months} months of development. Also in-sample.`,
  );
  protected readonly pairMaps = computed(() => this.study().results.search?.pair_maps ?? []);
  /** Each landscape's valid and invalid cells, counted from the server's cells. */
  protected readonly audits = computed(() => this.pairMaps().map((map) => pairAudit(map, this.capability())));

  constructor() {
    reloadOnProgress(this.revision, this.charts, () => this.request() !== undefined);
  }

  protected retry(): void {
    this.charts.reload();
  }
}
