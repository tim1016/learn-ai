import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, resource, signal } from '@angular/core';

import { etIsoDate } from '../../shared/date/et-midnight';
import { DateRangePickerComponent, UNBOUNDED, type DateBounds, type DateRange, type QuickRangeOffer } from '../../shared/date-picker';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { OptionsGoldenSearchService, optionsSearchErrorMessage } from './options-golden-search.service';
import type { HistoryGuidanceRequest } from './options-golden-search.types';

@Component({
  selector: 'app-options-search-history',
  imports: [DateRangePickerComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-history.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchHistoryComponent {
  private readonly service = inject(OptionsGoldenSearchService);
  readonly retained = input.required<HistoryGuidanceRequest['bounds']>();
  /** Window lengths the rules form accepts; null while any of them is invalid. */
  readonly lengths = input.required<Omit<HistoryGuidanceRequest, 'bounds'> | null>();
  readonly range = input.required<DateRange>();
  readonly rangeChanged = output<DateRange>();
  protected readonly guidance = resource({
    params: () => {
      const lengths = this.lengths();
      return lengths === null ? undefined : { bounds: this.retained(), ...lengths };
    },
    loader: ({ params }) => this.service.historyGuidance(params),
  });
  protected readonly answer = computed(() => this.guidance.hasValue() ? this.guidance.value() : null);
  protected readonly problem = computed(() => this.guidance.error() === undefined ? null : optionsSearchErrorMessage(this.guidance.error(), 'Date guidance could not be loaded. Choose dates and check the plan, or retry.'));
  protected readonly bounds = computed<DateBounds>(() => {
    const bounds = this.retained();
    return { ...UNBOUNDED, min: bounds.start_ms === null ? null : etIsoDate(bounds.start_ms), max: bounds.end_ms === null ? null : etIsoDate(bounds.end_ms - 1), minReason: 'Retained underlying history begins here.', maxReason: 'Retained underlying history ends here.', unknown: bounds.start_ms === null || bounds.end_ms === null ? 'Retained history dates are unknown.' : null };
  });
  protected readonly maximum = computed<DateRange | null>(() => {
    const answer = this.answer();
    return answer === null || answer.start_ms === null || answer.end_ms === null ? null : { from: etIsoDate(answer.start_ms), to: etIsoDate(answer.end_ms - 1) };
  });
  protected readonly offers = computed<readonly QuickRangeOffer[]>(() => [{ key: 'Max', range: this.maximum(), unavailable: this.maximum() === null ? this.answer()?.explanation ?? 'Reading the available study dates.' : null, note: 'Longest retained interval that fits the training, forward and final windows.' }]);
  protected readonly isMaximum = computed(() => this.maximum()?.from === this.range().from && this.maximum()?.to === this.range().to);
  /** Whether the range tracks Max. The page mounts this with an empty range, so it starts out following. */
  private readonly following = signal(true);

  constructor() {
    effect(() => {
      const maximum = this.maximum();
      if (maximum !== null && this.following()) this.rangeChanged.emit(maximum);
    });
  }

  protected chooseRange(range: DateRange): void {
    this.following.set(false);
    this.rangeChanged.emit(range);
  }

  protected useMaximum(): void {
    this.following.set(true);
  }
}
