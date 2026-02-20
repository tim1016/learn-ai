import {
  Component,
  input,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { UIChart } from 'primeng/chart';
import { LstmFoldResult } from '../../../graphql/lstm-types';

@Component({
  selector: 'app-fold-metrics-chart',
  standalone: true,
  imports: [UIChart],
  template: `
    @if (chartData()) {
      <p-chart type="bar" [data]="chartData()" [options]="chartOptions" height="300px" />
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FoldMetricsChartComponent {
  foldResults = input<LstmFoldResult[]>([]);
  metric = input<'rmse' | 'directionalAccuracy'>('rmse');

  chartData = computed(() => {
    const folds = this.foldResults();
    if (!folds.length) return null;

    const m = this.metric();
    const labels = folds.map((f) => `Fold ${f.fold}`);
    const values = folds.map((f) => (m === 'rmse' ? f.rmse : f.directionalAccuracy * 100));

    const color = m === 'rmse' ? '#2196F3' : '#4CAF50';
    const bgColor = m === 'rmse' ? 'rgba(33,150,243,0.6)' : 'rgba(76,175,80,0.6)';

    return {
      labels,
      datasets: [
        {
          label: m === 'rmse' ? 'RMSE' : 'Directional Accuracy (%)',
          data: values,
          backgroundColor: bgColor,
          borderColor: color,
          borderWidth: 1,
        },
      ],
    };
  });

  chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx: any) => {
            const val = ctx.parsed.y?.toFixed(4);
            return `${ctx.dataset.label}: ${val}`;
          },
        },
      },
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { font: { size: 11 }, color: '#999' },
      },
      y: {
        grid: { color: '#f0f0f0' },
        ticks: { font: { size: 11 }, color: '#999' },
      },
    },
  };
}
