import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ButtonModule } from 'primeng/button';

import { fillModeLabel } from '../../models/fill-mode';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { reloadOnProgress } from './charts/golden-search-reload';
import { incumbentLabel, knobsByName, usesTradeFrequency } from './golden-search-display';
import { GoldenSearchActivityComponent } from './golden-search-activity.component';
import { GoldenSearchKnobTableComponent } from './golden-search-knob-table.component';
import {
  coverageSpec,
  minimumsHeight,
  minimumsSpec,
  searchSpaceHeight,
  searchSpaceSpec,
  windowMapHeight,
  windowMapSpec,
  workloadHeight,
  workloadSpec,
} from './golden-search-plan-charts';
import { GoldenSearchService } from './golden-search.service';
import type { PlanCharts, StrategyCapability, StudyDetail } from './golden-search.types';

/**
 * A locked study's frozen plan, read-only (#2696), with its receipt and the
 * way to change it: Revise starts a new linked study from this plan. The
 * study itself never changes. Its charts (#2821) — the plan at a glance, its
 * windows, search space, workload, trade minimums and the lake's coverage —
 * come from their own read, read again when the study's revision or its run count moves.
 */
@Component({
  selector: 'app-golden-search-plan-summary',
  imports: [
    ButtonModule,
    DecimalPipe,
    GoldenSearchActivityComponent,
    GoldenSearchChartComponent,
    GoldenSearchGridComponent,
    GoldenSearchKnobTableComponent,
    GoldenSearchPanelComponent,
    PercentPipe,
    ReceiptLabelPipe,
    RouterLink,
    TimestampDisplayComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-plan-summary.component.html',
  styleUrl: './golden-search-plan-summary.component.scss',
})
export class GoldenSearchPlanSummaryComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  private readonly service = inject(GoldenSearchService);
  private readonly studyId = computed(() => this.study().id);
  /** What moves the charts: a new revision, or a running stage reserving runs (which leaves the revision alone). */
  private readonly progress = computed(() => `${this.study().revision}:${this.study().consumed_evaluations}`);
  private readonly charts = resource({
    params: () => ({ studyId: this.studyId() }),
    loader: ({ params }) => this.service.planCharts(params.studyId),
  });
  protected readonly chartData = computed<PlanCharts | null>(() => (this.charts.hasValue() ? this.charts.value() : null));
  protected readonly chartsError = computed(() => {
    const error = this.charts.error();
    return error === undefined ? null : extractServerMessage(error, 'The plan charts could not be loaded.');
  });
  protected readonly windowMap = computed(() => this.spec(windowMapSpec), { equal: sameChart });
  protected readonly searchSpace = computed(() => this.spec(searchSpaceSpec), { equal: sameChart });
  protected readonly workload = computed(() => this.spec(workloadSpec), { equal: sameChart });
  protected readonly minimums = computed(() => this.spec(minimumsSpec), { equal: sameChart });
  protected readonly coverage = computed(
    () => {
      const coverage = this.chartData()?.coverage;
      return coverage?.status === 'measured' ? coverageSpec(coverage.months) : null;
    },
    { equal: sameChart },
  );
  protected readonly coverageNote = computed(() => {
    const coverage = this.chartData()?.coverage;
    return coverage?.status === 'missing' ? coverage.reason : null;
  });
  protected readonly heights = computed(() => {
    const charts = this.chartData();
    return {
      windows: windowMapHeight(charts?.windows.length ?? 0),
      space: searchSpaceHeight(charts?.search_space.length ?? 0),
      workload: workloadHeight(charts?.workload.stages.length ?? 0),
      minimums: minimumsHeight(charts?.minimums.windows.length ?? 0),
    };
  });
  /** The method tile's detail line. */
  protected readonly methodDetail = computed(() => {
    const protocol = this.study().protocol;
    if (protocol.method !== 'zoom') return 'Every combination of the searched values';
    const count = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
    return `${count(protocol.zoom.points, 'point')} · ${count(protocol.zoom.refinements, 'refinement')} · ${protocol.zoom.passes === 1 ? '1 pass' : `${protocol.zoom.passes} passes`}, one knob at a time`;
  });
  /** The development and final-test windows, for the tiles. */
  protected readonly tileWindows = computed(() => {
    const windows = this.chartData()?.windows ?? [];
    return { development: windows.find((window) => window.kind === 'development') ?? null, final: windows.find((window) => window.kind === 'final') ?? null };
  });

  protected readonly fillMode = computed(() => fillModeLabel(this.study().protocol.execution.fill_mode));
  protected readonly canRevise = computed(() => this.study().permitted_actions.includes('revise'));
  protected readonly frequencyBased = computed(() => usesTradeFrequency(this.study().protocol));
  protected readonly pairs = computed(() => {
    const knobs = knobsByName(this.capability());
    return this.study().protocol.pair_audits.map(([a, b]) => `${knobs.get(a)?.label ?? a} × ${knobs.get(b)?.label ?? b}`);
  });
  protected readonly incumbent = computed(() => incumbentLabel(this.study().protocol.incumbent));

  constructor() {
    reloadOnProgress(this.progress, this.charts);
  }

  protected retry(): void {
    this.charts.reload();
  }

  private spec<T>(build: (charts: PlanCharts) => T): T | null {
    const charts = this.chartData();
    return charts === null ? null : build(charts);
  }
}
