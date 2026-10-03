import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { DecimalPipe, NgTemplateOutlet } from '@angular/common';

import { extractServerMessage } from '../broker/operation-error';
import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import type { ChartSpec } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { pairAudit } from './golden-search-compare';
import { GoldenSearchPairLandscapeComponent } from './golden-search-pair-landscape.component';
import { GoldenSearchProcedureComponent } from './golden-search-procedure.component';
import { convergenceSpec, eligibilityMapSpec, knobMovesSpec, profilesMinWidth, profilesSpec } from './golden-search-search-charts';
import { GoldenSearchService } from './golden-search.service';
import type { ProcedureCharts, SearchCharts, StrategyCapability, StudyDetail } from './golden-search.types';

interface ChartRequest {
  readonly studyId: string;
  readonly revision: number;
}

/** One procedure's charts as the step lays them out. */
export interface ProcedureChartsView {
  /** Null for the all-period search; names the recent fit's panels apart from it. */
  readonly instance: string | null;
  readonly zoom: boolean;
  readonly path: ChartSpec | null;
  /** Why a Zoom path is not drawn, in the server's words. */
  readonly pathNote: string | null;
  readonly moves: ChartSpec | null;
  readonly profiles: ChartSpec | null;
  readonly profilesMinWidth: number;
  readonly eligibility: ChartSpec | null;
}

const LABELS: Readonly<Record<ProcedureCharts['key'], string>> = { search: 'All-period search', recent: 'Recent window' };

/**
 * The Search step (#2696): the all-period procedure fitted on the whole
 * development interval, the recent-window procedure when the plan asked for
 * one, and the study's engine-run accounting. Their charts (#2821) — the
 * path Zoom took, each knob's move and profile, every point scored against
 * the rules, and the pair landscape — come from their own read, once per
 * study revision. Everything here is development evidence: it chose the
 * settings.
 */
@Component({
  selector: 'app-golden-search-search-step',
  imports: [
    DecimalPipe,
    GoldenSearchChartComponent,
    GoldenSearchGridComponent,
    GoldenSearchPairLandscapeComponent,
    GoldenSearchPanelComponent,
    GoldenSearchProcedureComponent,
    NgTemplateOutlet,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-search-step.component.html',
  styleUrl: './golden-search-search-step.component.scss',
})
export class GoldenSearchSearchStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  private readonly service = inject(GoldenSearchService);

  private readonly request = computed<ChartRequest | undefined>(
    () => (this.study().results.search === null ? undefined : { studyId: this.study().id, revision: this.study().revision }),
    { equal: (a, b) => a?.studyId === b?.studyId && a?.revision === b?.revision },
  );
  private readonly charts = resource({
    params: () => this.request(),
    loader: ({ params }) => this.service.searchCharts(params.studyId),
  });
  private readonly chartData = computed<SearchCharts | null>(() => (this.charts.hasValue() ? this.charts.value() : null));
  protected readonly chartsError = computed(() => {
    const error = this.charts.error();
    return error === undefined ? null : extractServerMessage(error, 'The search charts could not be loaded.');
  });
  /** Each recorded procedure's charts, by key. */
  protected readonly views = computed(() => {
    const capability = this.capability();
    return new Map((this.chartData()?.procedures ?? []).map((procedure) => [procedure.key, viewOf(procedure, capability)] as const));
  });

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
  protected readonly audits = computed(() => this.pairMaps().map((map) => pairAudit(map, this.capability())));

  protected retry(): void {
    this.charts.reload();
  }
}

function viewOf(procedure: ProcedureCharts, capability: StrategyCapability | null): ProcedureChartsView {
  const label = LABELS[procedure.key];
  const convergence = procedure.convergence;
  return {
    instance: procedure.key === 'search' ? null : label,
    zoom: procedure.method === 'zoom',
    path: convergence.status === 'measured' ? convergenceSpec(procedure, convergence.tried, capability, label) : null,
    pathNote: convergence.status === 'missing' ? convergence.reason : null,
    moves: procedure.moves.length === 0 ? null : knobMovesSpec(procedure, label),
    profiles: procedure.profiles.length === 0 ? null : profilesSpec(procedure, label),
    profilesMinWidth: profilesMinWidth(procedure.profiles.length),
    eligibility: procedure.points.length === 0 ? null : eligibilityMapSpec(procedure, capability, label),
  };
}
