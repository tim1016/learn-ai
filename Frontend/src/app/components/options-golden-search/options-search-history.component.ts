import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, resource, untracked } from '@angular/core';

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
  readonly request = input.required<HistoryGuidanceRequest>();
  readonly range = input.required<DateRange>();
  readonly rangeChanged = output<DateRange>();
  protected readonly validLengths = computed(() => {
    const request = this.request();
    return Number.isInteger(request.train_months) && request.train_months >= 1 && request.train_months <= 120 && [request.test_months, request.final_months].every(value => Number.isInteger(value) && value >= 1 && value <= 60);
  });
  protected readonly guidance = resource({ params: () => this.validLengths() ? this.request() : undefined, loader: ({ params }) => this.service.historyGuidance(params) });
  protected readonly answer = computed(() => this.guidance.hasValue() ? this.guidance.value() : null);
  protected readonly problem = computed(() => this.guidance.error() === undefined ? null : optionsSearchErrorMessage(this.guidance.error(), 'Date guidance could not be loaded. Choose dates and check the plan, or retry.'));
  protected readonly bounds = computed<DateBounds>(() => {
    const bounds = this.request().bounds;
    return { ...UNBOUNDED, min: bounds.start_ms === null ? null : etIsoDate(bounds.start_ms), max: bounds.end_ms === null ? null : etIsoDate(bounds.end_ms - 1), minReason: 'Retained underlying history begins here.', maxReason: 'Retained underlying history ends here.', unknown: bounds.start_ms === null || bounds.end_ms === null ? 'Retained history dates are unknown.' : null };
  });
  protected readonly maximum = computed<DateRange | null>(() => {
    const answer = this.answer();
    return answer === null || answer.start_ms === null || answer.end_ms === null ? null : { from: etIsoDate(answer.start_ms), to: etIsoDate(answer.end_ms - 1) };
  });
  protected readonly offers = computed<readonly QuickRangeOffer[]>(() => [{ key: 'Max', range: this.maximum(), unavailable: this.maximum() === null ? this.answer()?.explanation ?? 'Reading the available study dates.' : null, note: 'Longest retained interval that fits the training, forward and final windows.' }]);
  protected readonly isMaximum = computed(() => this.maximum()?.from === this.range().from && this.maximum()?.to === this.range().to);
  private initialized = false;
  private automatic: DateRange | null = null;

  constructor() {
    effect(() => {
      const maximum = this.maximum();
      if (maximum === null) return;
      const current = untracked(this.range);
      if ((!this.initialized && current.from === '' && current.to === '') || (this.automatic !== null && current.from === this.automatic.from && current.to === this.automatic.to)) {
        this.automatic = maximum;
        this.rangeChanged.emit(maximum);
      }
      this.initialized = true;
    });
  }

  protected chooseRange(range: DateRange): void {
    this.initialized = true;
    this.automatic = null;
    this.rangeChanged.emit(range);
  }

  protected useMaximum(): void {
    const maximum = this.maximum();
    if (maximum === null) return;
    this.automatic = maximum;
    this.initialized = true;
    this.rangeChanged.emit(maximum);
  }
}
