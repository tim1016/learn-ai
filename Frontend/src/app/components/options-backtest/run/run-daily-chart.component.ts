import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import { formatAuthoredUsd } from '../../../shared/pipes/authored-usd.pipe';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import type { DayRow, Drawdown, RunFigures } from '../options-backtest.types';
import { dayRequestFrom, type DayRequest } from '../run-detail/day-request';
import { equityCoordinates, isLoss, scaledBarPercents } from './run-chart-scale';

@Component({
  selector: 'app-options-backtest-run-daily-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-daily-chart.component.html',
  styleUrl: './run-daily-chart.component.scss',
})
export class RunDailyChartComponent {
  readonly rows = input.required<readonly DayRow[]>();
  readonly equityCurve = input<RunFigures['equity_curve'] | undefined>(undefined);
  readonly drawdown = input<Drawdown | null>(null);
  readonly openDay = output<DayRequest>();

  protected readonly curve = computed(() => {
    const points = this.equityCurve() ?? [];
    if (points.length === 0) return null;
    const coordinates = equityCoordinates(points.map((point) => point.equity_usd));
    const plotted = points.map((point, index) => ({
      ...coordinates[index],
      openMs: point.session_open_ms,
      label: point.session_open_ms === null
        ? `Starting cash: ${formatAuthoredUsd(point.equity_usd)}`
        : `${formatTimestampDisplay(point.session_open_ms, { mode: 'date-et', granularity: 'date' })}: equity ${formatAuthoredUsd(point.equity_usd)}`,
    }));
    const peakMs = this.drawdown()?.peak_session_open_ms ?? null;
    const troughMs = this.drawdown()?.trough_session_open_ms;
    const peak = peakMs === null ? plotted[0] : plotted.find((point) => point.openMs === peakMs);
    const trough = plotted.find((point) => point.openMs === troughMs);
    return {
      points: plotted,
      line: plotted.map((point) => `${point.x},${point.y}`).join(' '),
      shade: peak && trough ? `${peak.x},0 ${trough.x},0 ${trough.x},160 ${peak.x},160` : null,
    };
  });

  protected readonly bars = computed(() => {
    const rows = this.rows();
    const heights = scaledBarPercents(rows.map((row) => row.net_profit_usd), 50);
    return rows.map((row, index) => ({
      openMs: row.session_open_ms,
      heightCss: `${heights[index]}%`,
      loss: isLoss(row.net_profit_usd),
      noTrade: row.trades === 0,
      label: `${formatTimestampDisplay(row.session_open_ms, { mode: 'date-et', granularity: 'date' })}: ${formatAuthoredUsd(row.net_profit_usd)}`,
      buttonLabel: `Look at ${formatTimestampDisplay(row.session_open_ms, { mode: 'date-et', granularity: 'date' })}: ${formatAuthoredUsd(row.net_profit_usd)}`,
    }));
  });
  protected readonly columns = computed(() => `repeat(${this.bars().length}, minmax(5px, 1fr))`);

  protected readonly firstDate = computed(() => formatTimestampDisplay(this.rows()[0]?.session_open_ms, { mode: 'date-et', granularity: 'short-date' }));
  protected readonly lastDate = computed(() => formatTimestampDisplay(this.rows().at(-1)?.session_open_ms, { mode: 'date-et', granularity: 'short-date' }));

  protected ask(openMs: number, event: Event): void {
    this.openDay.emit(dayRequestFrom(openMs, event));
  }
}
