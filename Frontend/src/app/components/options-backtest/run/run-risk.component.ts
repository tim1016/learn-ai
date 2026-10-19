import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { RunFigures } from '../options-backtest.types';
import { equityPlot, isLoss, scaledBarPercents, sessionX } from './run-chart-scale';

type Dashboard = NonNullable<RunFigures['dashboard']>;

@Component({
  selector: 'app-options-backtest-run-risk',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-risk.component.html',
  styleUrl: './run-risk.component.scss',
})
export class RunRiskComponent {
  readonly dashboard = input<Dashboard | null>(null);
  protected readonly rolling = computed(() => {
    const days = this.dashboard()?.daily ?? [];
    const observed = days.filter((day) => day.rolling_sharpe !== null && day.rolling_sharpe !== undefined);
    const scale = equityPlot(observed.map((day) => day.rolling_sharpe ?? '0'));
    if (!scale) return null;
    const coordinates = new Map(observed.map((day, index) => [day.session_open_ms, scale.coordinates[index].y]));
    const segments: string[] = [];
    let current: string[] = [];
    days.forEach((day, index) => {
      const y = coordinates.get(day.session_open_ms);
      if (y === undefined) { if (current.length) segments.push(current.join(' ')); current = []; }
      else current.push(`${sessionX(index + 1, days.length)},${y}`);
    });
    if (current.length) segments.push(current.join(' '));
    const labels = new Map(observed.map((day) => [day.rolling_sharpe, day.display_rolling_sharpe ?? day.rolling_sharpe]));
    return { high: labels.get(scale.high) ?? scale.high, low: labels.get(scale.low) ?? scale.low, segments };
  });
  protected readonly bounds = computed(() => ({ first: this.dashboard()?.histogram.at(0)?.lower_usd, last: this.dashboard()?.histogram.at(-1)?.upper_usd }));
  protected readonly bins = computed(() => {
    const bins = this.dashboard()?.histogram ?? [];
    const heights = scaledBarPercents(bins.map((bin) => String(bin.count)), 100);
    return bins.map((bin, index) => ({ ...bin, height: heights[index], loss: isLoss(bin.lower_usd) && (isLoss(bin.upper_usd) || /^0+(?:\.0+)?$/.test(bin.upper_usd)), label: `${bin.count} sessions: ${bin.lower_usd} to ${bin.upper_usd} USD` }));
  });
}
