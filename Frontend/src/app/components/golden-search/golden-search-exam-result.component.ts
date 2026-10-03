import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { evidenceRows } from './golden-search-decision';
import { metricTexts, percentText, type MetricTexts } from './golden-search-display';
import type { ExamView, Metrics, StudyDetail } from './golden-search.types';

interface ResultRow extends MetricTexts {
  readonly key: string;
  readonly label: string;
  readonly selected: boolean;
}

/** One side of the final test; a side with no result says so where a failed run gives its error. */
function resultRow(key: string, label: string, metrics: Metrics | null, selected: boolean): ResultRow {
  const figures = metricTexts(metrics);
  return { key, label, selected, ...figures, failure: metrics === null ? 'No result recorded' : figures.failure };
}

/**
 * The one final test, read once it ran (#2696): the locked candidate against
 * the frozen incumbent on the held-back interval, each check the frozen rules
 * made with its outcome, the descriptive retention (never a check), and the
 * four separate evidence rows — research, repeatability, independent engine
 * agreement, Golden acceptance. A proof failure is an alert: research
 * override cannot bypass it and the old default stands.
 */
@Component({
  selector: 'app-golden-search-exam-result',
  imports: [DecimalPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-exam-result.component.html',
  styleUrl: './golden-search-exam-result.component.scss',
})
export class GoldenSearchExamResultComponent {
  readonly study = input.required<StudyDetail>();
  readonly exam = input.required<ExamView>();
  readonly candidateLabel = input.required<string>();

  protected readonly rows = computed(() => [
    resultRow('candidate', this.candidateLabel(), this.exam().candidate_metrics, true),
    resultRow('incumbent', 'Frozen incumbent', this.exam().incumbent_metrics, false),
  ]);
  protected readonly evidence = computed(() => evidenceRows(this.study().state, this.exam()));
  protected readonly retention = computed(() => {
    const retention = this.exam().retention;
    return retention === null ? null : percentText(retention);
  });
  protected readonly proofFailure = computed(() => {
    const study = this.study();
    if (study.state !== 'qualification_failed') return null;
    return study.results.qualification?.failure_reason ?? study.failure_reason ?? 'The proof could not be built or verified.';
  });
}
