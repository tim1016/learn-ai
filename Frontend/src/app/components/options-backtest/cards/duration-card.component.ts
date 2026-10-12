import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { DateRangePickerComponent, type DateRange } from '../../../shared/date-picker';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { OFFERED_RANGES, priceBounds, type RangeChange } from '../duration';

/**
 * The dates a run covers, and from when option prices are available. The
 * dates are the picker's own `YYYY-MM-DD` and are not part of the setup. The
 * line beside them is the server's word, from the last Check data answer, and
 * its date is also the picker's lower bound, with the reason in words.
 */
@Component({
  selector: 'app-options-backtest-duration-card',
  imports: [DateRangePickerComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './duration-card.component.html',
  styleUrl: '../options-backtest-card.scss',
})
export class DurationCardComponent {
  readonly range = input.required<DateRange>();
  /** The first day option prices are available from (`option_prices_from_ms` of a Check data answer); null until one has run. */
  readonly optionPricesFromMs = input<number | null>(null);
  /** The server's reason it refused the range, as served. */
  readonly problem = input<string | null>(null);
  /** Set when the page changed the range on the owner's click (Run available dates). */
  readonly rangeChange = input<RangeChange | null>(null);
  readonly changed = output<DateRange>();

  protected readonly bounds = computed(() => priceBounds(this.optionPricesFromMs()));
  protected readonly quickRanges = OFFERED_RANGES;
}
