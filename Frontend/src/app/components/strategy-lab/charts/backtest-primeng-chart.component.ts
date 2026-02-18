import {
  Component, input, computed, effect, signal,
  ChangeDetectionStrategy
} from '@angular/core';
import { UIChart } from 'primeng/chart';
import { BacktestTrade } from '../../../graphql/types';

@Component({
  selector: 'app-backtest-primeng-chart',
  standalone: true,
  imports: [UIChart],
  template: `
    <div class="primeng-chart-wrapper">
      @if (chartData()) {
        <p-chart
          type="line"
          [data]="chartData()"
          [options]="chartOptions()"
          [height]="height() + 'px'"
        />
      }
    </div>
  `,
  styles: [`
    .primeng-chart-wrapper {
      background: white;
      border: 1px solid #e9ecef;
      border-radius: 8px;
      padding: 12px;
    }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BacktestPrimengChartComponent {
  trades = input<BacktestTrade[]>([]);
  height = input(300);

  chartData = computed(() => {
    const t = this.trades();
    if (!t.length) return null;

    const labels = t.map((_, i) => `Trade ${i + 1}`);
    const values = t.map(tr => tr.cumulativePnl);
    const isPositive = values.length > 0 && values[values.length - 1] >= 0;

    const lineColor = isPositive ? '#2e7d32' : '#c0392b';
    const bgColor = isPositive ? 'rgba(46,125,50,0.12)' : 'rgba(192,57,43,0.12)';

    return {
      labels,
      datasets: [
        {
          label: 'Cumulative P&L ($)',
          data: values,
          fill: true,
          borderColor: lineColor,
          backgroundColor: bgColor,
          tension: 0.3,
          pointRadius: t.length <= 40 ? 3 : 0,
          pointHoverRadius: 5,
          pointBackgroundColor: lineColor,
          borderWidth: 2,
        },
      ],
    };
  });

  chartOptions = computed(() => {
    return {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        mode: 'index' as const,
        intersect: false,
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx: any) => `P&L: $${ctx.parsed.y?.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: {
          display: true,
          grid: { display: false },
          ticks: {
            maxTicksLimit: 8,
            font: { size: 11 },
            color: '#999',
          },
        },
        y: {
          display: true,
          grid: { color: '#f0f0f0' },
          ticks: {
            font: { size: 11 },
            color: '#999',
            callback: (value: number) => `$${value.toFixed(2)}`,
          },
        },
      },
    };
  });
}
