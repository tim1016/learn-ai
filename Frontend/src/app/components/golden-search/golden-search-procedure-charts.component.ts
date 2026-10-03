import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { GoldenSearchPairLandscapeComponent } from './golden-search-pair-landscape.component';
import { convergenceSpec, eligibilityMapSpec, knobMovesSpec, profilesMinWidth, profilesSpec } from './golden-search-search-charts';
import type { PairMap, Point, ProcedureCharts, StrategyCapability } from './golden-search.types';

const LABELS: Readonly<Record<ProcedureCharts['key'], string>> = { search: 'All-period', recent: 'Recent window' };

/**
 * One Search procedure's panels (#2821): the path Zoom took, each knob's move
 * and profile, every point scored against the rules, and — for the
 * all-period search — the pair landscape. The recent fit's panels are named
 * apart (the panel's instance). Each chart stays drawn while a reload brings
 * the same values.
 */
@Component({
  selector: 'app-golden-search-procedure-charts',
  imports: [GoldenSearchChartComponent, GoldenSearchGridComponent, GoldenSearchPairLandscapeComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-procedure-charts.component.html',
  styleUrl: './golden-search-procedure-charts.component.scss',
})
export class GoldenSearchProcedureChartsComponent {
  readonly procedure = input.required<ProcedureCharts>();
  readonly capability = input<StrategyCapability | null>(null);
  /** The pair maps to draw beside this procedure; empty for the recent fit. */
  readonly maps = input<readonly PairMap[]>([]);
  readonly center = input<Point | null>(null);

  private readonly label = computed(() => LABELS[this.procedure().key]);
  /** Null for the all-period search; names the recent fit's panels apart from it. */
  protected readonly instance = computed(() => (this.procedure().key === 'search' ? null : this.label()));
  protected readonly path = computed(
    () => {
      const procedure = this.procedure();
      return procedure.convergence.status === 'measured' ? convergenceSpec(procedure, procedure.convergence.tried, this.capability(), this.label()) : null;
    },
    { equal: sameChart },
  );
  protected readonly pathNote = computed(() => {
    const convergence = this.procedure().convergence;
    return convergence.status === 'missing' ? convergence.reason : null;
  });
  protected readonly moves = computed(() => (this.procedure().moves.length === 0 ? null : knobMovesSpec(this.procedure(), this.label())), { equal: sameChart });
  protected readonly profiles = computed(() => (this.procedure().profiles.length === 0 ? null : profilesSpec(this.procedure(), this.label())), { equal: sameChart });
  protected readonly profilesMinWidth = computed(() => profilesMinWidth(this.procedure().profiles.length));
  /** Failed runs have no numbers to place on the eligibility map; the caption counts them. */
  protected readonly failedRuns = computed(() => this.procedure().points.filter((point) => point.ineligibility === 'FAILED').length);
  protected readonly eligibility = computed(
    () => (this.procedure().points.length === 0 ? null : eligibilityMapSpec(this.procedure(), this.capability(), this.label())),
    { equal: sameChart },
  );
}
