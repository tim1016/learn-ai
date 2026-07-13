import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import type {
  EngineValidationAnalytics,
  SeasonalityMonth,
  TimingCell,
} from '../engine-validation-analytics.types';

interface TimingRow {
  weekday: number;
  label: string;
  cells: (TimingCell | null)[];
}

interface SeasonalityBar {
  month: SeasonalityMonth;
  heightPct: number;
  positive: boolean;
}

interface StabilityBar {
  tradeNumber: number;
  heightPct: number;
  positive: boolean;
  averageReturn: number;
}

@Component({
  selector: 'app-validation-atlas',
  imports: [TimestampDisplayComponent],
  templateUrl: './validation-atlas.component.html',
  styleUrl: './validation-atlas.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidationAtlasComponent {
  readonly analytics = input.required<EngineValidationAnalytics>();

  readonly timingHours = computed(() =>
    [...new Set(this.analytics().timing_cells.map((cell) => cell.hour_et))].sort((a, b) => a - b),
  );

  readonly timingRows = computed<TimingRow[]>(() => {
    const analytics = this.analytics();
    const hours = this.timingHours();
    const lookup = new Map(
      analytics.timing_cells.map((cell) => [`${cell.weekday}:${cell.hour_et}`, cell]),
    );
    return ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].map((label, weekday) => ({
      weekday,
      label,
      cells: hours.map((hour) => lookup.get(`${weekday}:${hour}`) ?? null),
    }));
  });

  readonly seasonalityBars = computed<SeasonalityBar[]>(() => {
    const months = this.analytics().seasonality;
    const values = months.map((month) => Math.abs(month.median_compounded_return ?? 0));
    const max = Math.max(...values, 0);
    return months.map((month) => {
      const value = month.median_compounded_return ?? 0;
      return {
        month,
        heightPct: max > 0 ? Math.max(4, (Math.abs(value) / max) * 46) : 0,
        positive: value >= 0,
      };
    });
  });

  readonly stabilityBars = computed<StabilityBar[]>(() => {
    const points = this.analytics().rolling_trade_stability;
    const max = Math.max(...points.map((point) => Math.abs(point.average_return)), 0);
    return points.map((point) => ({
      tradeNumber: point.trade_number,
      heightPct: max > 0 ? Math.max(3, (Math.abs(point.average_return) / max) * 46) : 0,
      positive: point.average_return >= 0,
      averageReturn: point.average_return,
    }));
  });

  timingClass(cell: TimingCell | null): string {
    if (cell === null) return 'empty';
    if (cell.average_return > 0) return 'positive';
    if (cell.average_return < 0) return 'negative';
    return 'flat';
  }

  formatHour(hour: number): string {
    return `${hour.toString().padStart(2, '0')}:00`;
  }

  formatPercent(value: number | null): string {
    if (value === null) return '—';
    const sign = value > 0 ? '+' : '';
    return `${sign}${(value * 100).toFixed(2)}%`;
  }

  formatRatio(value: number | null): string {
    return value === null ? '—' : value.toFixed(2);
  }
}
