import { afterNextRender, ChangeDetectionStrategy, Component, computed, effect, inject, Injector, input, output, signal, untracked, viewChildren } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import type { GoldenSearchChartId } from './charts/golden-search-chart-guides';
import { sameChart, type ChartSpec } from './charts/golden-search-chart-spec';
import { GoldenSearchGridComponent } from './charts/golden-search-grid.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { GoldenSearchReviewTour } from './charts/golden-search-review-tour';
import type { CandidateRow } from './golden-search-compare';
import { concentrationCurveSpec, withoutBestSpec } from './golden-search-concentration-charts';
import { monthCalendarSpec, monthlyNetSpec } from './golden-search-month-charts';
import { GoldenSearchPairLandscapeComponent } from './golden-search-pair-landscape.component';
import { entryRsiSpec, entryTimeSpec, histogramSpec, holdTimeSpec, tradeTimelineSpec } from './golden-search-trade-charts';
import type { CandidateDetail, CandidateKey, MeasuredTradeCharts, PairMap, Point, StrategyCapability } from './golden-search.types';

export type EvidenceTab = 'map' | 'months' | 'trades';

export const EVIDENCE_TABS: readonly { id: EvidenceTab; label: string }[] = [
  { id: 'map', label: 'Parameter map' },
  { id: 'months', label: 'By month' },
  { id: 'trades', label: 'Trades' },
];


/**
 * The Compare step's evidence (#2696): the parameter map; the selected
 * candidate's months, with how much of its result rests on its best month or
 * trades (#2815); and its trades over time, in bins, by hold, by RSI at entry
 * and by entry time (#2821). The detail runs come from the step, which reads
 * them for its charts. Everything shown is development evidence, used to
 * choose.
 */
@Component({
  selector: 'app-golden-search-evidence-tabs',
  imports: [GoldenSearchChartComponent, GoldenSearchGridComponent, GoldenSearchPairLandscapeComponent, GoldenSearchPanelComponent],
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
  private readonly review = inject(GoldenSearchReviewTour, { optional: true });
  private readonly panels = viewChildren(GoldenSearchPanelComponent);

  protected readonly tabs = EVIDENCE_TABS;
  readonly active = signal<EvidenceTab>('map');

  protected readonly selectedRun = computed(() => this.details()?.get(this.selected().key)?.development ?? null);
  // Each chart is equal while it draws the same values, so a study poll does not redraw it.
  protected readonly monthCalendar = computed(
    () => {
      const months = this.selectedRun()?.monthly ?? [];
      return months.length === 0 ? null : monthCalendarSpec(this.selected().candidate, months);
    },
    { equal: sameChart },
  );
  /** A row per year of the calendar. */
  protected readonly calendarHeight = computed(() => 40 + 34 * new Set((this.selectedRun()?.monthly ?? []).map((month) => month.year)).size);
  protected readonly monthlyNet = computed(
    () => {
      const months = this.selectedRun()?.monthly ?? [];
      return months.length === 0 ? null : monthlyNetSpec(this.selected().candidate, months);
    },
    { equal: sameChart },
  );
  /** The trade charts when the server drew them; `tradesNote` says why not. */
  private readonly tradeCharts = computed(() => {
    const charts = this.selectedRun()?.trade_charts;
    return charts?.status === 'measured' ? charts : null;
  });
  protected readonly tradesNote = computed(() => {
    const charts = this.selectedRun()?.trade_charts;
    return charts?.status === 'missing' ? charts.reason : null;
  });
  protected readonly timeline = computed(() => this.tradeChart(tradeTimelineSpec), { equal: sameChart });
  protected readonly histogram = computed(() => this.tradeChart(histogramSpec), { equal: sameChart });
  protected readonly holdTime = computed(() => this.tradeChart(holdTimeSpec), { equal: sameChart });
  protected readonly entryTime = computed(() => this.tradeChart(entryTimeSpec), { equal: sameChart });
  /** Why hold times are not counted, in the server's words. */
  protected readonly holdNote = computed(() => this.tradeCharts()?.hold_reason ?? null);
  protected readonly entryRsi = computed(
    () => {
      const charts = this.tradeCharts();
      return charts?.entry_rsi.status === 'measured' ? entryRsiSpec(this.selected().candidate, charts, charts.entry_rsi) : null;
    },
    { equal: sameChart },
  );
  protected readonly entryRsiNote = computed(() => {
    const rsi = this.tradeCharts()?.entry_rsi;
    return rsi?.status === 'missing' ? rsi.reason : null;
  });
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
      return measure.status === 'missing' ? null : withoutBestSpec(candidate, measure);
    },
    { equal: sameChart },
  );
  /** Why "Without its best" draws nothing, in the server's words. */
  protected readonly withoutBestNote = computed(() => {
    const measure = this.selected().candidate.concentration;
    return measure.status === 'missing' ? measure.reason : null;
  });

  private tradeChart(spec: (candidate: CandidateRow['candidate'], charts: MeasuredTradeCharts) => ChartSpec | null): ChartSpec | null {
    const charts = this.tradeCharts();
    return charts === null ? null : spec(this.selected().candidate, charts);
  }

  constructor() {
    // The study review opens the tab that holds the chart it is on.
    effect(() => {
      const tab = this.review?.stop()?.tab ?? null;
      if (tab !== null) untracked(() => this.open(tab));
    });
  }

  open(tab: EvidenceTab): void {
    this.active.set(tab);
  }

  /** Opens `tab` and moves focus to the `chart` panel it holds once the panel shows. */
  showChart(chart: GoldenSearchChartId, tab: EvidenceTab): void {
    this.open(tab);
    afterNextRender(() => this.panels().find((panel) => panel.chart() === chart)?.focusHeading(), { injector: this.injector });
  }
}
