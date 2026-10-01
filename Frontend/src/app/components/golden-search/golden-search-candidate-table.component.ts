import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { CandidateRow } from './golden-search-compare';
import type { CandidateKey } from './golden-search.types';

/**
 * The Compare step's candidate table (#2696): one row per distinct setting —
 * the searches' fits and the frozen incumbent, candidates that are the same
 * settings folded together — with the development net return, worst fall
 * (and the server's flag when it breaks the ceiling), trades and Sharpe.
 * The first column picks the candidate; once the final test is opened the
 * pick is fixed. At phone width each row reads as labelled lines.
 */
@Component({
  selector: 'app-golden-search-candidate-table',
  imports: [DecimalPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-candidate-table.component.html',
  styleUrl: './golden-search-candidate-table.component.scss',
})
export class GoldenSearchCandidateTableComponent {
  readonly rows = input.required<readonly CandidateRow[]>();
  readonly selected = input<CandidateKey | null>(null);
  /** The final test was opened: the pick can no longer change. */
  readonly fixed = input(false);
  readonly capital = input.required<number>();
  readonly pick = output<CandidateKey>();
}
