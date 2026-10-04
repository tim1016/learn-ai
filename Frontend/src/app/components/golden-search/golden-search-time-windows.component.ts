import { ChangeDetectionStrategy, Component, computed, inject, input, output, untracked } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { etIsoDate } from '../../shared/date/et-midnight';
import { DateRangePickerComponent, type DataGaps, type DateBounds, type QuickRangeKey } from '../../shared/date-picker';
import { DEFAULT_ADJUSTMENT_MODE, HeldGapsService, TickerCatalogService, heldBoundsFor } from '../../shared/ticker-catalog';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { EXPOSURE_CLAIMS, EXPOSURE_LABELS } from './golden-search-display';
import { numberProblemKey, type PlanEdit, type ProtocolNumberField, type StudyRange } from './golden-search-plan-draft';
import { RANGE_INPUT_ID, numberInputId, type PlanProblem } from './golden-search-plan-problems';
import type { GoldenSearchPreflight, ProtocolRequest } from './golden-search.types';

interface Span {
  readonly key: string;
  /** Left edge and width as percentages of the development part of the bar. */
  readonly left: number;
  readonly width: number;
}

/** Where `ms` falls between `start` and `end`, as a percentage clamped to the bar. */
function percentAlong(ms: number, start: number, end: number): number {
  return Math.min(100, Math.max(0, ((ms - start) / (end - start)) * 100));
}

/**
 * The plan's time windows (#2696): the fold and final-test lengths in months,
 * the study range the server lays the dates out inside (#2886), with what it
 * left out of the range and its refusal of a date under the picker, and a
 * bar showing development, the folds' test windows and the sealed final test
 * as the server laid them out, with the final test's exposure and the warmup
 * every run is primed with.
 */
@Component({
  selector: 'app-golden-search-time-windows',
  imports: [InputText, TimestampDisplayComponent, DateRangePickerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-time-windows.component.html',
  styleUrls: ['./golden-search-plan-card.scss', './golden-search-time-windows.component.scss'],
})
export class GoldenSearchTimeWindowsComponent {
  private readonly catalog = inject(TickerCatalogService);
  private readonly heldGaps = inject(HeldGapsService);

  readonly protocol = input.required<ProtocolRequest>();
  /** The study range picked on the form; undefined until one is, and the picker then shows the plan's own first and last day. */
  readonly range = input<StudyRange | undefined>(undefined);
  /** Which sessions of the picked range the plan leaves out, in the server's words; null when it uses them all. */
  readonly rangeNote = input<string | null | undefined>(undefined);
  /** The final test's length in months; undefined while the dates do not follow a month count. */
  readonly finalMonths = input<number | undefined>(undefined);
  /** Trailing sessions the server left out of the final test because the lake has not reached them; undefined with the month count. */
  readonly finalSessionsCut = input<number | undefined>(undefined);
  /** The server's answer to the current plan: its folds, exposure and run-up; null until checked. */
  readonly preflight = input<GoldenSearchPreflight | null>(null);
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  /** The server's refusals of the plan; the ones that name a date are shown under the range. */
  readonly refusals = input<readonly PlanProblem[]>([]);
  readonly edit = output<PlanEdit>();

  protected readonly numberId = numberInputId;
  protected readonly rangeId = RANGE_INPUT_ID;
  /** A study is months long, so the day and week ranges are left out. */
  protected readonly quickRanges: readonly QuickRangeKey[] = ['6M', '1Y', '2Y', 'Max'];
  protected readonly exposureLabels = EXPOSURE_LABELS;
  protected readonly exposureClaims = EXPOSURE_CLAIMS;
  protected readonly shown = computed<StudyRange>(() => {
    const p = this.protocol();
    return this.range() ?? { from: etIsoDate(p.development_start_ms), to: etIsoDate(p.final_end_ms - 1) };
  });
  /** The days a study of this instrument can read: what the default tree holds for it. */
  protected readonly bounds = computed<DateBounds>(() => {
    const symbol = this.protocol().symbol;
    // `viewFor` may install a resource effect, which a reactive context forbids.
    return heldBoundsFor(untracked(() => this.catalog.viewFor(DEFAULT_ADJUSTMENT_MODE)), symbol);
  });
  /** The sessions inside those days the tree lacks: a held span is only a first and a last day. */
  protected readonly gaps = computed<DataGaps | null>(() => this.heldGaps.gapsFor(DEFAULT_ADJUSTMENT_MODE, this.protocol().symbol, this.bounds(), 'minute'));
  /** The server's refusals of a date, in its own words. */
  protected readonly rangeRefusals = computed(() => this.refusals().filter((refusal) => refusal.target === RANGE_INPUT_ID));

  /** Where the final test starts along the bar, and each fold's test window within development; null when the dates are out of order. */
  protected readonly timeline = computed<{ readonly finalAt: number; readonly folds: readonly Span[] } | null>(() => {
    const p = this.protocol();
    if (!(p.development_start_ms < p.final_start_ms && p.final_start_ms < p.final_end_ms)) return null;
    const inDevelopment = (ms: number): number => percentAlong(ms, p.development_start_ms, p.final_start_ms);
    const folds = (this.preflight()?.folds ?? []).map((fold) => {
      const left = inDevelopment(fold.test_start_ms);
      return { key: String(fold.fold_index), left, width: inDevelopment(fold.test_end_ms) - left };
    });
    return { finalAt: percentAlong(p.final_start_ms, p.development_start_ms, p.final_end_ms), folds };
  });

  protected numberInvalid(field: ProtocolNumberField): boolean {
    return this.problems().has(numberProblemKey(field));
  }

  protected onNumber(field: ProtocolNumberField, raw: string): void {
    this.edit.emit({ kind: 'number', field, raw });
  }

  protected onRange(range: StudyRange): void {
    this.edit.emit({ kind: 'range', from: range.from, to: range.to });
  }
}
