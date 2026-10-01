import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import { entryText, pointDifferences } from './golden-search-display';
import { GoldenSearchFoldTableComponent, type FoldRow } from './golden-search-fold-table.component';
import { GoldenSearchLinkedLineComponent } from './golden-search-linked-line.component';
import type { LinkedFoldReturn, StrategyCapability, StudyDetail } from './golden-search.types';

interface LinkedRow extends LinkedFoldReturn {
  /** The frozen incumbent's linked return through the same fold; null once its line is broken. */
  readonly benchmark: number | null;
}

/**
 * The Test over time step (#2696): each fold re-ran the frozen procedure on
 * its own training window from the original ranges and starting point, then
 * tested only that winner on the next months. Shows every fold (failures
 * included) beside the frozen incumbent on the same test window (the
 * benchmark), the legacy verdict with its coverage, and both linked
 * test-period return lines with their table alternative. It judges the
 * selection procedure, not any single candidate.
 */
@Component({
  selector: 'app-golden-search-test-step',
  imports: [DecimalPipe, GoldenSearchFoldTableComponent, GoldenSearchLinkedLineComponent, PercentPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-test-step.component.html',
  styleUrl: './golden-search-test-step.component.scss',
})
export class GoldenSearchTestStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);

  protected readonly validation = computed(() => this.study().results.validation);
  protected readonly rows = computed<FoldRow[]>(() => {
    const study = this.study();
    const seed = study.protocol.seed ?? study.protocol.incumbent.params;
    return (study.results.validation?.folds ?? []).map((fold) => {
      if (fold.winner === null) return { fold, changes: null };
      const changed = pointDifferences(fold.winner, seed, this.capability());
      return { fold, changes: changed.length === 0 ? 'Same as the starting point' : changed.map(entryText).join(' · ') };
    });
  });
  protected readonly linkedRows = computed<LinkedRow[]>(() => {
    const validation = this.study().results.validation;
    if (validation === null) return [];
    const benchmark = new Map(validation.incumbent_linked.map((point) => [point.fold_index, point.linked_return]));
    return validation.linked.map((point) => ({ ...point, benchmark: benchmark.get(point.fold_index) ?? null }));
  });
  protected readonly pending = computed(() =>
    this.study().state === 'validation_running'
      ? 'The folds are running; each appears here when the stage finishes.'
      : 'The procedure has not been tested over time yet. It runs after the search.',
  );
}
