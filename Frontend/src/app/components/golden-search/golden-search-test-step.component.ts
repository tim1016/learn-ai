import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { entryText, pointDifferences } from './golden-search-display';
import { GoldenSearchLinkedLineComponent } from './golden-search-linked-line.component';
import { GoldenSearchMetricsComponent } from './golden-search-metrics.component';
import type { StrategyCapability, StudyDetail, ValidationFold } from './golden-search.types';

interface FoldRow {
  readonly fold: ValidationFold;
  /** The fold winner's settings that differ from the frozen starting point, or null when no winner was chosen. */
  readonly changes: string | null;
}

/**
 * The Test over time step (#2696): each fold re-ran the frozen procedure on
 * its own training window from the original ranges and starting point, then
 * tested only that winner on the next months. Shows every fold (failures
 * included), the legacy verdict with its coverage, and the linked test-period
 * return line with its table alternative. It judges the selection procedure,
 * not any single candidate.
 */
@Component({
  selector: 'app-golden-search-test-step',
  imports: [DecimalPipe, GoldenSearchLinkedLineComponent, GoldenSearchMetricsComponent, PercentPipe, ReceiptLabelPipe, TimestampDisplayComponent],
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
  protected readonly pending = computed(() =>
    this.study().state === 'validation_running'
      ? 'The folds are running; each appears here when the stage finishes.'
      : 'The procedure has not been tested over time yet. It runs after the search.',
  );
}
