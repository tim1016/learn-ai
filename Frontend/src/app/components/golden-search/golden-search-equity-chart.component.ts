import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { GoldenSearchChartComponent } from './charts/golden-search-chart.component';
import { GoldenSearchPanelComponent } from './charts/golden-search-panel.component';
import type { CandidateRow } from './golden-search-compare';
import { equityChartSpec, type EquityLine } from './golden-search-equity-chart';
import type { CandidateDetail, CandidateKey, StudyScope } from './golden-search.types';

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

  protected readonly spec = computed(() => {
    const details = this.details();
    if (details === null) return null;
    const lines: EquityLine[] = this.rows().map((row) => {
      const run = details.get(row.key)?.development ?? null;
      return { key: row.key, label: row.candidate.label, returns: run?.cumulative_return ?? [], falls: run?.drawdown ?? [] };
    });
    return equityChartSpec(lines, this.selected(), this.ceiling());
  });
}
