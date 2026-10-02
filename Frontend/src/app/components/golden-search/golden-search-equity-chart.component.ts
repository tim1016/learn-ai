import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import type { CandidateRow } from './golden-search-compare';
import { equityChartSpec, type EquityLine } from './golden-search-equity-chart';
import type { CandidateDetail, CandidateKey, CumulativeReturnPoint, DrawdownPoint, StudyScope } from './golden-search.types';

/** One shared empty list each, so a candidate with no run compares equal from poll to poll. */
const NO_RETURNS: readonly CumulativeReturnPoint[] = [];
const NO_FALLS: readonly DrawdownPoint[] = [];

/**
 * Compare's equity panel (#2821, V19): every candidate's development
 * cumulative return and its fall from peak, the selected candidate drawn
 * bold, read from the candidates' detail runs.
 */
@Component({
  selector: 'app-golden-search-equity-chart',
  imports: [DecimalPipe, GoldenSearchChartComponent, GoldenSearchPanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-equity-chart.component.html',
  styleUrl: './golden-search-equity-chart.component.scss',
})
export class GoldenSearchEquityChartComponent {
  readonly rows = input.required<readonly CandidateRow[]>();
  /** The candidates' detail runs; null while they load. */
  readonly details = input.required<ReadonlyMap<CandidateKey, CandidateDetail> | null>();
  readonly error = input<string | null>(null);
  readonly selected = input.required<CandidateKey>();
  /** The frozen worst-fall limit, a fraction of peak equity. */
  readonly ceiling = input.required<number>();
  readonly scope = input.required<StudyScope>();
  readonly retry = output();

  /** Each row's series; equal while the same runs are drawn, so a study poll (new rows, same runs) does not redraw. */
  private readonly lines = computed<EquityLine[] | null>(
    () => {
      const details = this.details();
      if (details === null) return null;
      return this.rows().map((row) => {
        const run = details.get(row.key)?.development ?? null;
        return { key: row.key, label: row.candidate.label, returns: run?.cumulative_return ?? NO_RETURNS, falls: run?.drawdown ?? NO_FALLS };
      });
    },
    { equal: sameLines },
  );

  protected readonly spec = computed(() => {
    const lines = this.lines();
    return lines === null ? null : equityChartSpec(lines, this.selected(), this.ceiling());
  });
}

function sameLines(a: readonly EquityLine[] | null, b: readonly EquityLine[] | null): boolean {
  if (a === null || b === null) return a === b;
  return a.length === b.length && a.every((line, i) => line.key === b[i].key && line.label === b[i].label && line.returns === b[i].returns && line.falls === b[i].falls);
}
