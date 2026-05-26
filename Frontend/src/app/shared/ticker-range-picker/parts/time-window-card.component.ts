import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import {
  daysBetween,
  dominantState,
  isoDate,
  summarizeAvailability,
  weekdaysBetween,
  type AvailabilityCell,
  type DominantState,
  type TickerRange,
} from '../ticker-range-picker.types';
import { toMostRecentWeekday } from '../../date/weekday';

export type LegendTreatment = 'tinted-bold' | 'solid-bold' | 'icon-glyph';

interface Preset {
  days: number;
  label: string;
}

const PRESETS: readonly Preset[] = [
  { days: 7, label: '7D' },
  { days: 30, label: '1M' },
  { days: 90, label: '3M' },
  { days: 180, label: '6M' },
  { days: 365, label: '1Y' },
  { days: 730, label: '2Y' },
];

@Component({
  selector: 'app-time-window-card',
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './time-window-card.component.html',
  styleUrls: ['./time-window-card.component.scss'],
})
export class TimeWindowCardComponent {
  readonly value = model.required<TickerRange>();
  readonly availability = input<readonly AvailabilityCell[]>([]);
  readonly legendTreatment = input<LegendTreatment>('tinted-bold');

  readonly presets = PRESETS;

  readonly summary = computed(() => summarizeAvailability(this.availability()));
  readonly dominant = computed<DominantState>(() => dominantState(this.summary()));

  readonly spanDays = computed(() => {
    const v = this.value();
    return daysBetween(v.from, v.to);
  });

  readonly spanBusinessDays = computed(() => {
    const summaryDays = this.summary().weekdays;
    if (summaryDays > 0) return summaryDays;
    const v = this.value();
    return weekdaysBetween(v.from, v.to);
  });

  readonly activePreset = computed(() => {
    const s = this.spanDays();
    return PRESETS.find((p) => Math.abs(s - p.days) < 2)?.days ?? null;
  });

  trackByDay(_: number, c: AvailabilityCell): string {
    return c.date;
  }

  updateFrom(v: string): void {
    this.value.set({ ...this.value(), from: v });
  }

  updateTo(v: string): void {
    this.value.set({ ...this.value(), to: v });
  }

  applyPreset(days: number): void {
    const end = new Date();
    end.setHours(0, 0, 0, 0);
    const start = new Date(end);
    start.setDate(start.getDate() - days);
    // Both endpoints must land on a trading weekday — the sidecar
    // validator rejects weekend ``start_ms_utc`` or ``end_ms_utc``
    // with a 422. The 1M/1Y/2Y presets reliably land on a weekend
    // when today is the matching DOW; without this guard the form
    // submits a value the server is guaranteed to reject.
    this.value.set({
      ...this.value(),
      from: isoDate(toMostRecentWeekday(start)),
      to: isoDate(toMostRecentWeekday(end)),
    });
  }
}
