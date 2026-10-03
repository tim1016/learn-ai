import { ChangeDetectionStrategy, Component, computed, effect, input, output, viewChild } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { DatePicker } from 'primeng/datepicker';

import { fromCalendarDate, toCalendarDate, type IsoDate } from '../date/iso-date';
import { UNBOUNDED, type DateBounds } from './date-bounds';
import type { DateRange } from './quick-ranges';
import type { TradingSessions } from './trading-sessions';

const WEEKEND = [0, 6];

/**
 * The month grid behind both date controls: PrimeNG's calendar, speaking
 * calendar dates instead of viewer-local ``Date`` objects.
 *
 * Days outside the bounds cannot be picked. Given a trading calendar,
 * weekends and holidays cannot be picked either. In range mode the second
 * click completes the range; a single date is a range of one day.
 */
@Component({
  selector: 'app-date-calendar',
  imports: [DatePicker, ReactiveFormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <p-datepicker
      [inline]="true"
      [formControl]="control"
      [selectionMode]="mode()"
      [numberOfMonths]="mode() === 'range' ? 2 : 1"
      [firstDayOfWeek]="1"
      [minDate]="minDate()"
      [maxDate]="maxDate()"
      [disabledDays]="sessions() === null ? [] : weekend"
      [disabledDates]="closedDates()"
      (onSelect)="onSelect()"
    />
  `,
})
export class DateCalendarComponent {
  readonly mode = input<'single' | 'range'>('single');
  /** The selection shown: the single date, or the range's first day. Empty for none. */
  readonly from = input<IsoDate | ''>('');
  /** The range's last day; ignored for a single date. */
  readonly to = input<IsoDate | ''>('');
  readonly bounds = input<DateBounds>(UNBOUNDED);
  /** The trading calendar when only sessions may be picked; null lets every day through. */
  readonly sessions = input<TradingSessions | null>(null);
  /** A completed pick. A single date arrives with ``from`` equal to ``to``. */
  readonly picked = output<DateRange>();

  protected readonly weekend = WEEKEND;
  protected readonly control = new FormControl<Date | (Date | null)[] | null>(null);

  private readonly first = computed(() => this.bounds().min ?? this.sessions()?.from ?? null);
  private readonly last = computed(() => this.bounds().max ?? this.sessions()?.through ?? null);
  protected readonly minDate = computed(() => asCalendarDate(this.first()));
  protected readonly maxDate = computed(() => asCalendarDate(this.last()));
  protected readonly closedDates = computed<Date[]>(() => {
    const sessions = this.sessions();
    if (sessions === null) return [];
    return sessions.holidaysBetween(this.first() ?? sessions.from, this.last() ?? sessions.through).map(toCalendarDate);
  });

  private readonly picker = viewChild(DatePicker);

  constructor() {
    effect(() => {
      const range = this.mode() === 'range';
      const from = asCalendarDate(this.from() || null);
      const to = asCalendarDate(this.to() || null);
      this.control.setValue(range ? (from === null ? null : [from, to]) : from, { emitEvent: false });
      this.showMonthOf(this.picker(), this.to() || this.from() || this.bounds().max, range);
    });
  }

  /**
   * Opens on the month that matters. PrimeNG puts a range's last day in the
   * left of its two months, which spends the right one on days past the end
   * of the bound; this puts it on the right. With no date and no last day to
   * show, PrimeNG's own choice (today) stands.
   */
  private showMonthOf(picker: DatePicker | undefined, anchor: IsoDate | null, range: boolean): void {
    if (picker === undefined || anchor === null || anchor === '') return;
    const [year, month] = anchor.split('-').map(Number);
    const first = new Date(year, month - (range ? 2 : 1), 1);
    picker.currentMonth = first.getMonth();
    picker.currentYear = first.getFullYear();
    picker.createMonths(picker.currentMonth, picker.currentYear);
  }

  protected onSelect(): void {
    const value = this.control.value;
    if (value instanceof Date) {
      const day = fromCalendarDate(value);
      this.picked.emit({ from: day, to: day });
      return;
    }
    const [start, end] = value ?? [];
    if (start instanceof Date && end instanceof Date) this.picked.emit({ from: fromCalendarDate(start), to: fromCalendarDate(end) });
  }
}

function asCalendarDate(date: IsoDate | null): Date | null {
  return date === null ? null : toCalendarDate(date);
}
