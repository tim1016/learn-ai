import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DatePickerModule } from 'primeng/datepicker';

import { InstrumentCardComponent } from '../ticker-range-picker/parts/instrument-card.component';
import type {
  TickerOption,
  TickerRange,
} from '../ticker-range-picker/ticker-range-picker.types';
import type { TickerSnapshot } from './ticker-date-picker.types';

/**
 * Single ticker + single date picker for snapshot tools.
 *
 * Reuses the canonical InstrumentCard via a TickerRange projection —
 * the symbol field passes through normally; ``from`` and ``to`` are
 * both pinned to the snapshot's single ``date`` so InstrumentCard's
 * "snap to last 30 days of cache on pick" snapping is functionally
 * a no-op (we ignore the range half of the patched value).
 */
@Component({
  selector: 'app-ticker-date-picker',
  imports: [CommonModule, FormsModule, DatePickerModule, InstrumentCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './ticker-date-picker.component.html',
  styleUrls: ['./ticker-date-picker.component.scss'],
})
export class TickerDatePickerComponent {
  readonly value = model.required<TickerSnapshot>();
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);
  readonly minDate = input<Date | null>(null);
  readonly maxDate = input<Date | null>(null);
  readonly title = input('Snapshot');
  readonly dateLabel = input('Date');
  readonly idPrefix = input('tdp');

  /** Project the snapshot onto a TickerRange shape so InstrumentCard
   *  can two-way-bind. ``from`` and ``to`` collapse to ``date`` so the
   *  snap-to-30-days behavior is a no-op (we ignore the range half on
   *  patch-back). */
  protected readonly rangeProjection = computed<TickerRange>(() => {
    const v = this.value();
    return {
      symbol: v.symbol,
      from: v.date,
      to: v.date,
      resolution: 'daily',
    };
  });

  protected onInstrumentPatch(r: TickerRange): void {
    if (r.symbol !== this.value().symbol) {
      this.value.set({ ...this.value(), symbol: r.symbol });
    }
  }

  protected get dateValue(): Date | null {
    const s = this.value().date;
    if (!s) return null;
    const [y, m, d] = s.split('-').map(Number);
    if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) {
      return null;
    }
    return new Date(y, m - 1, d);
  }

  protected onDateChange(d: Date | null): void {
    if (!d) return;
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    this.value.set({ ...this.value(), date: `${y}-${m}-${day}` });
  }
}
