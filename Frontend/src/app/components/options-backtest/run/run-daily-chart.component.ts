import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import { AuthoredUsdPipe, formatAuthoredUsd } from '../../../shared/pipes/authored-usd.pipe';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import type { DayRow, Drawdown, RunFigures } from '../options-backtest.types';
import { monthLabel } from '../run-words';
import { dayRequestFrom, type DayRequest } from '../run-detail/day-request';
import { barMagnitude, dailyBarGeometry, equityPlot, isLoss, sessionPlotWidth, sessionX } from './run-chart-scale';

type DailyMetrics = NonNullable<RunFigures['dashboard']>['daily'];

/** One scrolling session axis. Exact authored money is used for labels and readouts. */
@Component({
  selector: 'app-options-backtest-run-daily-chart',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-daily-chart.component.html',
  styleUrl: './run-daily-chart.component.scss',
})
export class RunDailyChartComponent {
  readonly rows = input.required<readonly DayRow[]>();
  readonly equityCurve = input<RunFigures['equity_curve'] | undefined>(undefined);
  readonly drawdown = input<Drawdown | null>(null);
  readonly dailyMetrics = input<DailyMetrics | undefined>(undefined);
  readonly openDay = output<DayRequest>();
  protected readonly selected = signal<number | null>(null);
  protected readonly help = signal(false);
  protected readonly timeline = computed(() => {
    const rows = this.rows(), equity = this.equityCurve(), daily = this.dailyMetrics();
    const matchesRows = (points: readonly { session_open_ms: number | null }[]) =>
      points.length === rows.length && rows.every((row, index) => points[index]?.session_open_ms === row.session_open_ms);
    const equityAligned = !equity || equity.length === rows.length + 1 &&
      equity[0]?.session_open_ms === null && matchesRows(equity.slice(1));
    const dailyAligned = !daily || matchesRows(daily);
    return {
      rows,
      equity: equityAligned ? equity ?? null : null,
      daily: dailyAligned ? daily ?? null : null,
      alignmentIssue: !equityAligned || !dailyAligned
        ? 'Recorded chart series do not align by session; the affected path is unavailable.' : null,
    };
  });
  protected readonly plotWidth = computed(() => sessionPlotWidth(this.timeline().rows.length));
  protected readonly magnitude = computed(() => barMagnitude(this.timeline().rows.map((row) => row.net_profit_usd)));
  protected readonly curve = computed(() => {
    const { rows, equity: source } = this.timeline();
    if (!source) return null;
    const scale = equityPlot(source.map((point) => point.equity_usd));
    if (!scale) return null;
    const points = source.map((point, index) => ({
      ...point, x: sessionX(index, rows.length),
      y: scale.coordinates[index].y,
      label: point.session_open_ms === null ? `Starting cash: ${formatAuthoredUsd(point.equity_usd)}` : `${this.date(point.session_open_ms)}: equity ${formatAuthoredUsd(point.equity_usd)}`,
    }));
    const peakMs = this.drawdown()?.peak_session_open_ms ?? null;
    const peak = points.find((point) => point.session_open_ms === peakMs);
    const trough = points.find((point) => point.session_open_ms === this.drawdown()?.trough_session_open_ms);
    return { high: scale.high, low: scale.low, points, line: points.map((p) => `${p.x},${p.y}`).join(' '), shade: peak && trough ? `${peak.x},0 ${trough.x},0 ${trough.x},160 ${peak.x},160` : null };
  });
  protected readonly bars = computed(() => {
    const rows = this.timeline().rows, geometry = dailyBarGeometry(rows.map((row) => row.net_profit_usd));
    return rows.map((row, index) => {
      return { ...row, ...geometry[index], loss: isLoss(row.net_profit_usd), label: `Look at ${this.date(row.session_open_ms)}: ${formatAuthoredUsd(row.net_profit_usd)}` };
    });
  });
  protected readonly underwater = computed(() => {
    const { rows, daily } = this.timeline();
    if (!daily?.length) return null;
    const values = ['0', ...daily.map((point) => point.underwater_usd)];
    const scale = equityPlot(values);
    if (!scale) return null;
    const line = scale.coordinates.map((p, index) => `${sessionX(index, rows.length)},${p.y}`).join(' ');
    return { low: scale.low, zero: scale.coordinates[0].y, line, area: `${sessionX(0, rows.length)},${scale.coordinates[0].y} ${line} ${sessionX(rows.length, rows.length)},${scale.coordinates[0].y}` };
  });
  protected readonly months = computed(() => {
    const { rows, daily } = this.timeline();
    const seen = new Set<string>();
    return rows.flatMap((row, index) => {
      const day = daily?.[index];
      const label = day ? monthLabel(day.year, day.month) : this.date(row.session_open_ms).slice(0, 7);
      if (seen.has(label)) return [];
      seen.add(label);
      return [{ key: label, label, left: sessionX(index + 1, rows.length) / 10 }];
    });
  });
  protected readonly highlighted = computed(() => {
    const { rows, equity, daily } = this.timeline();
    const index = rows.findIndex((item) => item.session_open_ms === this.selected());
    const row = rows[index];
    if (!row) return null;
    return { ...row, date: this.date(row.session_open_ms), equity: equity?.[index + 1]?.equity_usd, underwater: daily?.[index]?.underwater_usd, x: sessionX(index + 1, rows.length) };
  });
  protected date(openMs: number): string { return formatTimestampDisplay(openMs, { mode: 'date-et', granularity: 'date' }); }
  protected ask(openMs: number, event: Event): void { this.selected.set(openMs); this.openDay.emit(dayRequestFrom(openMs, event)); }
}
