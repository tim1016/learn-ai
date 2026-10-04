import { ChangeDetectionStrategy, Component, Directive, ElementRef, computed, effect, inject, input, output, viewChild } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { DatePicker } from 'primeng/datepicker';

import { fromCalendarDate, toCalendarDate, type IsoDate } from '../date/iso-date';
import { UNBOUNDED, type DateBounds } from './date-bounds';
import type { DateRange } from './quick-ranges';
import type { TradingSessions } from './trading-sessions';

const WEEKEND = [0, 6];

let nextLegendId = 0;

/**
 * Ties a day PrimeNG draws to the text that describes it. PrimeNG labels the
 * focusable day cell itself, and a label replaces the text inside the cell
 * for a screen reader, so a mark is said as the cell's description.
 */
@Directive({ selector: '[appDayDescribedBy]' })
export class DayDescribedByDirective {
  /** The id of the describing text, or null for a day with nothing to add. */
  readonly describedBy = input.required<string | null>({ alias: 'appDayDescribedBy' });

  constructor() {
    const content = inject<ElementRef<HTMLElement>>(ElementRef).nativeElement;
    effect(() => {
      const id = this.describedBy();
      const cell = content.parentElement;
      if (cell === null) return;
      if (id === null) cell.removeAttribute('aria-describedby');
      else cell.setAttribute('aria-describedby', id);
    });
  }
}

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
  imports: [DatePicker, ReactiveFormsModule, DayDescribedByDirective],
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
    >
      <ng-template #date let-date>
        <span [class.date-calendar__unheld]="isMarked(date)" [appDayDescribedBy]="isMarked(date) ? legendId : null">{{ date.day }}</span>
      </ng-template>
    </p-datepicker>
    @if ((marked()?.size ?? 0) > 0) {
      <p class="date-calendar__legend"><span class="date-calendar__unheld" aria-hidden="true">12</span> <span [id]="legendId">A session the lake does not hold.</span></p>
    }
  `,
  styleUrl: './date-calendar.component.scss',
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
  /** Sessions the host's data lacks: still pickable, underlined so a gap is seen before it is picked across. */
  readonly marked = input<ReadonlySet<IsoDate> | null>(null);
  /** A completed pick. A single date arrives with ``from`` equal to ``to``. */
  readonly picked = output<DateRange>();

  protected readonly weekend = WEEKEND;
  /** The legend's sentence, which a marked day points at as its description. */
  protected readonly legendId = `date-calendar-legend-${nextLegendId++}`;
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

  /** Whether the day PrimeNG is drawing (its month is zero-based) is one the host's data lacks. */
  protected isMarked(day: { readonly year: number; readonly month: number; readonly day: number }): boolean {
    const marked = this.marked();
    if (marked === null || marked.size === 0) return false;
    const two = (value: number): string => String(value).padStart(2, '0');
    return marked.has(`${day.year}-${two(day.month + 1)}-${two(day.day)}`);
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
