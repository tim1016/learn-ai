import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
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
import type { StudyDetail, TestOverTimeCharts } from './golden-search.types';

interface ChartRequest {
  readonly studyId: string;
  readonly revision: number;
}

/**
 * The Test over time step (#2696): each fold re-ran the frozen procedure on
 * its own training window from the original ranges and starting point, then
 * tested only that winner on the next months, beside the frozen incumbent on
 * the same test window. Its charts (#2821) come from their own read, once per
 * study revision rather than every poll; before the step runs they show the
 * receipt's planned folds. It judges the selection procedure, not any single
 * candidate.
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

  private readonly service = inject(GoldenSearchService);

  protected readonly validation = computed(() => this.study().results.validation);
  private readonly request = computed<ChartRequest>(() => ({ studyId: this.study().id, revision: this.study().revision }), {
    equal: (a, b) => a.studyId === b.studyId && a.revision === b.revision,
  });
  private readonly charts = resource({
    params: () => this.request(),
    loader: ({ params }) => this.service.testOverTimeCharts(params.studyId),
  });
  protected readonly chartData = computed<TestOverTimeCharts | null>(() => (this.charts.hasValue() ? this.charts.value() : null));
  protected readonly chartsError = computed(() => {
    const error = this.charts.error();
    return error === undefined ? null : extractServerMessage(error, 'The test-over-time charts could not be loaded.');
  });

  // Each chart is equal while it draws the same values, so a refetch that changed nothing does not redraw it.
  protected readonly timeline = computed(() => this.spec(foldTimelineSpec), { equal: sameChart });
  protected readonly linked = computed(() => this.spec(linkedReturnSpec), { equal: sameChart });
  protected readonly sharpe = computed(() => this.spec(sharpeRetentionSpec), { equal: sameChart });
  protected readonly drift = computed(() => (this.chartData()?.drift.length ? this.spec(parameterDriftSpec) : null), { equal: sameChart });
  protected readonly returns = computed(() => this.spec(foldReturnsSpec), { equal: sameChart });
  protected readonly activity = computed(() => this.spec(foldActivitySpec), { equal: sameChart });
  protected readonly driftHeight = computed(() => driftHeight(this.chartData()?.drift.length ?? 0));
  protected readonly timelineHeight = computed(() => 44 + 30 * (this.chartData()?.folds.length ?? 0));

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
