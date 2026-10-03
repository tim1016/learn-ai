import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';

import { DateRangePickerComponent, QUICK_RANGES, UNBOUNDED, type DateBounds, type QuickRangeKey } from '../../date-picker';
import type { TickerRange } from '../ticker-range-picker.types';

/** The dates of a ``TickerRange``, picked with the shared date range picker inside the picker family's card frame. */
@Component({
  selector: 'app-time-window-card',
  imports: [DateRangePickerComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './time-window-card.component.html',
  styleUrls: ['./time-window-card.component.scss'],
})
export class TimeWindowCardComponent {
  readonly value = model.required<TickerRange>();
  readonly appearance = input<'card' | 'flat'>('card');
  /** The days the host's run can read. */
  readonly bounds = input<DateBounds>(UNBOUNDED);
  /** The quick ranges offered; a host leaves out what makes no sense for it. */
  readonly ranges = input<readonly QuickRangeKey[]>(QUICK_RANGES);
  /** The quick range to start on when the host hands over no dates. */
  readonly fallback = input<QuickRangeKey | null>(null);

  protected setFrom(from: string): void {
    this.value.set({ ...this.value(), from });
  }

  protected setTo(to: string): void {
    this.value.set({ ...this.value(), to });
  }
}
