import {
  Component,
  ChangeDetectionStrategy,
  inject,
  computed,
  signal,
  model,
  input,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { DatePickerModule } from 'primeng/datepicker';
import { MessageModule } from 'primeng/message';

import { MarketMonitorService } from '../../services/market-monitor.service';
import {
  parseYmd,
  formatYmd,
  validateDateRange,
  getDisabledHolidayDates,
  getMinAllowedDate,
} from '../../utils/date-validation';
import type { MarketHolidayEvent } from '../../models/market-monitor';

@Component({
  selector: 'app-polygon-date-range',
  imports: [DatePickerModule, MessageModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './polygon-date-range.component.html',
  styleUrls: ['./polygon-date-range.component.scss'],
})
export class PolygonDateRangeComponent {
  fromDate = model.required<string>();
  toDate = model.required<string>();

  fromLabel = input<string>('From');
  toLabel = input<string>('To');
  idPrefix = input<string>('pdr');

  private readonly marketMonitor = inject(MarketMonitorService);
  private readonly holidays = signal<MarketHolidayEvent[]>([]);

  protected readonly minDate = new Date(getMinAllowedDate() + 'T00:00:00');
  protected readonly maxDate = (() => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return d;
  })();
  protected readonly disabledDays = [0, 6];
  protected readonly disabledDates = computed(() =>
    getDisabledHolidayDates(this.holidays()),
  );

  protected readonly fromDateValue = computed(() => parseYmd(this.fromDate()));
  protected readonly toDateValue = computed(() => parseYmd(this.toDate()));

  readonly warning = computed<string | null>(() =>
    validateDateRange(this.fromDate(), this.toDate()),
  );
  readonly valid = computed<boolean>(() => this.warning() === null);

  protected onFromChange(d: Date | null): void {
    this.fromDate.set(formatYmd(d));
  }
  protected onToChange(d: Date | null): void {
    this.toDate.set(formatYmd(d));
  }

  constructor() {
    firstValueFrom(this.marketMonitor.getHolidays(20))
      .then((events) => this.holidays.set(events))
      .catch(() => {
        /* non-critical, matches data-lab */
      });
  }
}
