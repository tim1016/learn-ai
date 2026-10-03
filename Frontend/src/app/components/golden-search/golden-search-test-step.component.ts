import { ChangeDetectionStrategy, Component, computed, effect, inject, input, resource, untracked } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import {
  driftHeight,
  foldActivitySpec,
  foldReturnsSpec,
  foldTimelineSpec,
  linkedReturnSpec,
  parameterDriftSpec,
  sharpeRetentionSpec,
} from './golden-search-test-charts';
import { GoldenSearchService } from './golden-search.service';
import type { StrategyCapability, StudyDetail, TestOverTimeCharts } from './golden-search.types';

/**
 * The Test over time step (#2696): each fold re-ran the frozen procedure on
 * its own training window from the original ranges and starting point, then
 * tested only that winner on the next months, beside the frozen incumbent on
 * the same test window. Its charts (#2821) come from their own read, read
 * again when the study's revision or a fold's status moves rather than on
 * every poll, keeping what is drawn while it reloads; before the step runs
 * they show the receipt's planned folds. It judges the selection procedure,
 * not any single candidate.
 */
@Component({
  selector: 'app-golden-search-test-step',
  imports: [DecimalPipe, GoldenSearchChartComponent, GoldenSearchGridComponent, GoldenSearchPanelComponent, PercentPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-test-step.component.html',
  styleUrl: './golden-search-test-step.component.scss',
})
export class GoldenSearchTestStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  private readonly service = inject(GoldenSearchService);

  protected readonly validation = computed(() => this.study().results.validation);
  private readonly studyId = computed(() => this.study().id);
  /** What the charts read: it moves with the revision and, during the stage, with each fold's status (fold writes keep the revision). */
  private readonly progress = computed(() => `${this.study().revision}|${(this.validation()?.folds ?? []).map((fold) => fold.status).join()}|${this.validation()?.verdict === null}`);
  private readonly charts = resource({
    params: () => ({ studyId: this.studyId() }),
    loader: ({ params }) => this.service.testOverTimeCharts(params.studyId),
  });
  protected readonly chartData = computed<TestOverTimeCharts | null>(() => (this.charts.hasValue() ? this.charts.value() : null));
  protected readonly chartsError = computed(() => {
    const error = this.charts.error();
    return error === undefined ? null : extractServerMessage(error, 'The test-over-time charts could not be loaded.');
  });

  // Each chart is equal while it draws the same values, so a refetch that changed nothing does not redraw it.
  protected readonly timeline = computed(() => this.spec((charts) => foldTimelineSpec(charts, this.capability())), { equal: sameChart });
  protected readonly linked = computed(() => (this.chartData()?.linked.length ? this.spec(linkedReturnSpec) : null), { equal: sameChart });
  protected readonly sharpe = computed(() => this.spec(sharpeRetentionSpec), { equal: sameChart });
  protected readonly drift = computed(() => (this.chartData()?.drift.length ? this.spec(parameterDriftSpec) : null), { equal: sameChart });
  protected readonly returns = computed(() => this.spec(foldReturnsSpec), { equal: sameChart });
  protected readonly activity = computed(() => this.spec(foldActivitySpec), { equal: sameChart });
  protected readonly driftHeight = computed(() => driftHeight(this.chartData()?.drift.length ?? 0));
  protected readonly timelineHeight = computed(() => 44 + 30 * (this.chartData()?.folds.length ?? 0));

  constructor() {
    // A reload keeps the drawn charts while it reads; a change of params would blank them.
    let seen: string | null = null;
    effect(() => {
      const progress = this.progress();
      if (seen !== null && progress !== seen) untracked(() => this.charts.reload());
      seen = progress;
    });
  }

  protected readonly pending = computed(() =>
    this.study().state === 'validation_running'
      ? 'The folds are running; each appears here when the stage finishes.'
      : 'The procedure has not been tested over time yet. It runs after the search.',
  );

  protected retry(): void {
    this.charts.reload();
  }

  private spec<T>(build: (charts: TestOverTimeCharts) => T): T | null {
    const charts = this.chartData();
    return charts === null || charts.folds.length === 0 ? null : build(charts);
  }
}
