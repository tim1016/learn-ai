import {
  Component, input, output, signal, computed, effect,
  ChangeDetectionStrategy,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePicker } from 'primeng/datepicker';
import { SharedModule } from 'primeng/api';
import { Tooltip } from 'primeng/tooltip';
import { MarketHolidayEvent } from '../../models/market-monitor';
import {
  buildHolidayMap,
  getDisabledHolidayDates,
  getMinAllowedDate,
} from '../../utils/date-validation';

@Component({
  selector: 'app-market-calendar',
  standalone: true,
  imports: [FormsModule, DatePicker, SharedModule, Tooltip],
  templateUrl: './market-calendar.component.html',
  styleUrls: ['./market-calendar.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class MarketCalendarComponent {
  // --- Inputs ---
  holidays = input<MarketHolidayEvent[]>([]);
  selectedDate = input<Date | null>(null);
  loading = input(false);

  // --- Outputs ---
  dateSelect = output<Date>();

  // --- Internal state ---
  internalDate = signal<Date | null>(null);

  // --- Computed ---
  disabledDates = computed(() => getDisabledHolidayDates(this.holidays()));
  holidayMap = computed(() => buildHolidayMap(this.holidays()));

  /** Disable weekends: Sunday = 0, Saturday = 6 */
  disabledDays: number[] = [0, 6];

  /** Polygon 2-year historical limit */
  minDate = new Date(getMinAllowedDate() + 'T00:00:00');
  maxDate = new Date();

  constructor() {
    effect(() => {
      const external = this.selectedDate();
      if (external) {
        this.internalDate.set(external);
      }
    });
  }

  onDateSelect(date: Date): void {
    this.internalDate.set(date);
    this.dateSelect.emit(date);
  }

  /**
   * Look up whether a calendar cell date is a holiday.
   * Month is 0-indexed from PrimeNG DatePickerDateMeta.
   */
  getHolidayForDate(day: number, month: number, year: number): MarketHolidayEvent | null {
    const m = String(month + 1).padStart(2, '0');
    const d = String(day).padStart(2, '0');
    return this.holidayMap().get(`${year}-${m}-${d}`) ?? null;
  }

  getHolidayTooltip(holiday: MarketHolidayEvent): string {
    let text = holiday.name ?? 'Market Holiday';
    if (holiday.status === 'Early Close' && holiday.close) {
      text += ` (Early Close)`;
    } else if (holiday.status) {
      text += ` - ${holiday.status}`;
    }
    return text;
  }
}
