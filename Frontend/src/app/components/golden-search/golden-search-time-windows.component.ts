import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { etIsoDate } from '../../shared/date/et-midnight';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { EXPOSURE_CLAIMS, EXPOSURE_LABELS } from './golden-search-display';
import { dateProblemKey, numberProblemKey, type PlanEdit, type ProtocolDateField, type ProtocolNumberField } from './golden-search-plan-draft';
import { dateInputId, numberInputId } from './golden-search-plan-problems';
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
 * The plan's time windows (#2696): the fold and final-test lengths in months
 * (the server lays the dates out from them), the dates themselves on
 * request, and a bar showing development, the folds' test windows and the
 * sealed final test as the server laid them out, with the final test's
 * exposure and the warmup every run is primed with.
 */
@Component({
  selector: 'app-golden-search-time-windows',
  imports: [InputText, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-time-windows.component.html',
  styleUrls: ['./golden-search-plan-card.scss', './golden-search-time-windows.component.scss'],
})
export class GoldenSearchTimeWindowsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  /** The final test's length in months; undefined while the dates do not follow a month count. */
  readonly finalMonths = input<number | undefined>(undefined);
  /** The server's answer to the current plan: its folds, exposure and run-up; null until checked. */
  readonly preflight = input<GoldenSearchPreflight | null>(null);
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly edit = output<PlanEdit>();

  protected readonly numberId = numberInputId;
  protected readonly dateId = dateInputId;
  protected readonly exposureLabels = EXPOSURE_LABELS;
  protected readonly exposureClaims = EXPOSURE_CLAIMS;
  protected readonly dates = computed(() => {
    const p = this.protocol();
    return { development_start: etIsoDate(p.development_start_ms), final_start: etIsoDate(p.final_start_ms), final_end: etIsoDate(p.final_end_ms - 1) };
  });

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

  protected dateInvalid(field: ProtocolDateField): boolean {
    return this.problems().has(dateProblemKey(field));
  }

  protected onNumber(field: ProtocolNumberField, raw: string): void {
    this.edit.emit({ kind: 'number', field, raw });
  }

  protected onDate(field: ProtocolDateField, raw: string): void {
    this.edit.emit({ kind: 'date', field, raw });
  }
}
