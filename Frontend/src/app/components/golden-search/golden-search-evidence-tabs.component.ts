import { afterNextRender, ChangeDetectionStrategy, Component, computed, inject, Injector, input, output, signal, viewChildren } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import type { CandidateRow } from './golden-search-compare';
import { concentrationCurveSpec, withoutBestSpec } from './golden-search-concentration-charts';
import { GoldenSearchMonthlyComponent } from './golden-search-monthly.component';
import { GoldenSearchPairMapComponent } from './golden-search-pair-map.component';
import { GoldenSearchTradesComponent } from './golden-search-trades.component';
import type { CandidateDetail, CandidateKey, PairMap, Point, StrategyCapability } from './golden-search.types';

export type EvidenceTab = 'map' | 'months' | 'trades';

export const EVIDENCE_TABS: readonly { id: EvidenceTab; label: string }[] = [
  { id: 'map', label: 'Parameter map' },
  { id: 'months', label: 'By month' },
  { id: 'trades', label: 'Trades' },
];

/** The chart panels each tab holds, which a summary row's "See the evidence" opens the tab to show. */
const TAB_CHARTS: ReadonlyMap<string, EvidenceTab> = new Map([
  ['concentration-curve', 'months'],
  ['without-best', 'months'],
]);

const NOT_MEASURED = 'Not measured for this study: its evidence was recorded before concentration was measured.';

/**
 * The Compare step's evidence (#2696): the parameter map and the selected
 * candidate's months — with how much of its result rests on its best month
 * or trades (#2815, #2821) — and its trades. The detail runs come from the
 * step, which reads them for its charts. Everything shown is development
 * evidence, used to choose.
 */
@Component({
  selector: 'app-golden-search-evidence-tabs',
  imports: [
    GoldenSearchChartComponent,
    GoldenSearchGridComponent,
    GoldenSearchMonthlyComponent,
    GoldenSearchPairMapComponent,
    GoldenSearchPanelComponent,
    GoldenSearchTradesComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-evidence-tabs.component.html',
  styleUrl: './golden-search-evidence-tabs.component.scss',
})
export class GoldenSearchEvidenceTabsComponent {
  readonly selected = input.required<CandidateRow>();
  /** The candidates' detail runs; null while they load. */
  readonly details = input.required<ReadonlyMap<CandidateKey, CandidateDetail> | null>();
  readonly detailsError = input<string | null>(null);
  readonly pairMaps = input.required<readonly PairMap[]>();
  /** The settings the pair maps are drawn through (the all-period fit). */
  readonly mapCenter = input<Point | null>(null);
  readonly mapCenterLabel = input.required<string>();
  readonly capability = input<StrategyCapability | null>(null);
  readonly retry = output();

  private readonly injector = inject(Injector);
  private readonly panels = viewChildren(GoldenSearchPanelComponent);

  protected readonly tabs = EVIDENCE_TABS;
  readonly active = signal<EvidenceTab>('map');

  protected readonly selectedRun = computed(() => this.details()?.get(this.selected().key)?.development ?? null);
  // Equal while they draw the same values, so a study poll does not redraw them.
  protected readonly curve = computed(
    () => {
      const curve = this.selectedRun()?.concentration_curve;
      return curve === null || curve === undefined || curve.reason !== null ? null : concentrationCurveSpec(this.selected().candidate, curve);
    },
    { equal: sameChart },
  );
  protected readonly withoutBest = computed(
    () => {
      const candidate = this.selected().candidate;
      const measure = candidate.concentration;
      return measure === null || measure.status === 'missing' ? null : withoutBestSpec(candidate, measure);
    },
    { equal: sameChart },
  );
  /** Why "Without its best" draws nothing: the measure's own reason, or evidence recorded before it existed. */
  protected readonly withoutBestNote = computed(() => this.selected().candidate.concentration?.reason ?? NOT_MEASURED);

  open(tab: EvidenceTab): void {
    this.active.set(tab);
  }

  /** Opens the tab holding `chart` and moves focus to its panel once it shows; false when no tab holds it. */
  showChart(chart: string): boolean {
    const tab = TAB_CHARTS.get(chart);
    if (tab === undefined) return false;
    this.open(tab);
    afterNextRender(() => this.panels().find((panel) => panel.chart() === chart)?.focusHeading(), { injector: this.injector });
    return true;
  }
}
