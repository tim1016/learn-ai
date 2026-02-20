import {
  Component,
  input,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { UIChart } from 'primeng/chart';

@Component({
  selector: 'app-training-history-chart',
  standalone: true,
  imports: [UIChart],
  template: `
    @if (chartData()) {
      <p-chart type="line" [data]="chartData()" [options]="chartOptions" height="300px" />
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TrainingHistoryChartComponent {
  historyLoss = input<number[]>([]);
  historyValLoss = input<number[]>([]);

  chartData = computed(() => {
    const loss = this.historyLoss();
    const valLoss = this.historyValLoss();
    if (!loss.length && !valLoss.length) return null;

    const maxLen = Math.max(loss.length, valLoss.length);
    const labels = Array.from({ length: maxLen }, (_, i) => `Epoch ${i + 1}`);

    return {
      labels,
      datasets: [
        {
          label: 'Training Loss',
          data: loss,
          borderColor: '#2196F3',
          backgroundColor: 'rgba(33,150,243,0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: loss.length <= 30 ? 3 : 0,
          borderWidth: 2,
        },
        {
          label: 'Validation Loss',
          data: valLoss,
          borderColor: '#FF9800',
          backgroundColor: 'rgba(255,152,0,0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: valLoss.length <= 30 ? 3 : 0,
          borderWidth: 2,
        },
      ],
    };
  });

  chartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index' as const, intersect: false },
    plugins: {
      legend: { position: 'top' as const },
      tooltip: {
        callbacks: {
          label: (ctx: any) => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(6)}`,
        },
      },
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { maxTicksLimit: 10, font: { size: 11 }, color: '#999' },
      },
      y: {
        grid: { color: '#f0f0f0' },
        title: { display: true, text: 'Loss (MSE)' },
        ticks: { font: { size: 11 }, color: '#999' },
      },
    },
  };
}
