import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { sameChart, type ChartSpec } from './charts/golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import { selectedCaption, type CandidateRow } from './golden-search-compare';
import { sparklineSpec } from './golden-search-compare-charts';
import type { CandidateDetail, CandidateKey } from './golden-search.types';

/**
 * The Compare step's candidate cards (#2821, V18): one card per distinct
 * setting — the searches' fits and the frozen incumbent, candidates that are
 * the same settings folded together — with whether it passes the plan's
 * rules, its settings, the development net return, Sharpe, worst fall and
 * trades against the development minimum (the server's flag beside a figure
 * that breaks a rule), and a small line of its development return. The
 * button on each card picks the candidate; once the final test is opened the
 * pick is fixed.
 */
@Component({
  selector: 'app-golden-search-candidate-cards',
  imports: [DecimalPipe, GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-candidate-cards.component.html',
  styleUrl: './golden-search-candidate-cards.component.scss',
})
export class GoldenSearchCandidateCardsComponent {
  readonly rows = input.required<readonly CandidateRow[]>();
  readonly selected = input<CandidateKey | null>(null);
  /** The final test was opened: the pick can no longer change. */
  readonly fixed = input(false);
  readonly capital = input.required<number>();
  /** The development trade minimum; null when the study does not carry it. */
  readonly floor = input<number | null>(null);
  /** The candidates' detail runs; null while they load. */
  readonly details = input.required<ReadonlyMap<CandidateKey, CandidateDetail> | null>();
  readonly pick = output<CandidateKey>();

  /** Each card's return line; equal while the same runs are drawn, so a study poll does not redraw them. */
  protected readonly lines = computed<ReadonlyMap<CandidateKey, ChartSpec>>(
    () => {
      const details = this.details();
      const lines = new Map<CandidateKey, ChartSpec>();
      for (const row of this.rows()) {
        const points = details?.get(row.key)?.development?.cumulative_return ?? [];
        if (points.length > 0) lines.set(row.key, sparklineSpec(row.key, row.candidate.label, points));
      }
      return lines;
    },
    { equal: (a, b) => a.size === b.size && [...a].every(([key, spec]) => sameChart(spec, b.get(key) ?? null)) },
  );
  protected readonly caption = computed(() => {
    const row = this.rows().find((candidate) => candidate.key === this.selected());
    return row === undefined ? null : selectedCaption(row.candidate);
  });
}
