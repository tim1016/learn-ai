import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { evidenceRows, meetsRules, outcomeLabel, type EvidenceTone } from './golden-search-decision';
import { metricTexts, percentText, type MetricTexts } from './golden-search-display';
import type { CandidateKey, ExamFit, ExamView, Metrics, StudyDetail } from './golden-search.types';

interface ResultRow extends MetricTexts {
  readonly key: CandidateKey;
  readonly label: string;
  readonly selected: boolean;
  /** The fit's own outcome; the incumbent is the benchmark and has none. */
  readonly outcome: { readonly text: string; readonly tone: EvidenceTone } | null;
}

/** One run of the final test; a run with no result says so where a failed run gives its error. */
function resultRow(key: CandidateKey, label: string, metrics: Metrics | null, selected: boolean, outcome: ResultRow['outcome']): ResultRow {
  const figures = metricTexts(metrics);
  return { key, label, selected, outcome, ...figures, failure: metrics === null ? 'No result recorded' : figures.failure };
}

/**
 * The one final test, read once it ran (#2696, #2930): every fit that sat it
 * against the frozen incumbent on the held-back interval, each fit with its
 * own outcome under the frozen rules. When two fits ran, the owner chooses
 * which one the rest of the page is about; that choice is fixed once an
 * approval names a fit. Below, for the chosen fit: the descriptive retention
 * (never a check) and the four separate evidence rows — research,
 * repeatability, independent engine agreement, Golden acceptance. A proof
 * failure is an alert: research override cannot bypass it and the old default
 * stands.
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
  /** The fit the page is about. */
  readonly fit = input.required<ExamFit>();
  readonly choose = output<CandidateKey>();

  /** A choice exists only between two fits, and only until an approval names one. */
  protected readonly choosable = computed(() => this.exam().fits.length > 1 && this.exam().chosen_key === null);
  protected readonly rows = computed(() => [
    ...this.exam().fits.map((fit) =>
      resultRow(fit.key, fit.label, fit.metrics, fit.key === this.fit().key, { text: outcomeLabel(fit), tone: meetsRules(fit) ? 'good' : 'warn' }),
    ),
    resultRow('incumbent', 'Frozen incumbent', this.exam().incumbent_metrics, false, null),
  ]);
  protected readonly evidence = computed(() => evidenceRows(this.study().state, this.exam(), this.fit()));
  protected readonly retention = computed(() => {
    const retention = this.fit().retention;
    return retention === null ? null : percentText(retention);
  });
  protected readonly proofFailure = computed(() => {
    const study = this.study();
    if (study.state !== 'qualification_failed') return null;
    return study.results.qualification?.failure_reason ?? study.failure_reason ?? 'The proof could not be built or verified.';
  });
}
