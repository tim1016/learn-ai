import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';

import { DateRangePickerComponent, UNBOUNDED, type DateBounds } from '../../date-picker';
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
  /** Whether both ends must be trading sessions. */
  readonly sessionsOnly = input(true);

  /** One write for both dates, so the host sees a change of range as one change. */
  protected setDates(dates: { readonly from: string; readonly to: string }): void {
    this.value.set({ ...this.value(), ...dates });
  }
}
