import {
  Component,
  input,
  computed,
  ChangeDetectionStrategy,
} from '@angular/core';
import { UIChart } from 'primeng/chart';

@Component({
  selector: 'app-residuals-chart',
  standalone: true,
  imports: [UIChart],
  template: `
    @if (chartData()) {
      <p-chart type="bar" [data]="chartData()" [options]="chartOptions" height="280px" />
    }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ResidualsChartComponent {
  residuals = input<number[]>([]);

  chartData = computed(() => {
    const residuals = this.residuals();
    if (!residuals.length) return null;

    // Bin residuals into a histogram
    const bins = this.createHistogram(residuals, 20);

    return {
      labels: bins.map((b) => b.label),
      datasets: [
        {
          label: 'Frequency',
          data: bins.map((b) => b.count),
          backgroundColor: bins.map((b) =>
            b.center >= 0 ? 'rgba(33,150,243,0.6)' : 'rgba(239,83,80,0.6)',
          ),
          borderColor: bins.map((b) =>
            b.center >= 0 ? '#2196F3' : '#ef5350',
          ),
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
          title: (ctx: any) => `Range: ${ctx[0]?.label}`,
          label: (ctx: any) => `Count: ${ctx.parsed.y}`,
        },
      },
    },
    scales: {
      x: {
        grid: { display: false },
        title: { display: true, text: 'Residual (Actual - Predicted)' },
        ticks: { maxTicksLimit: 10, font: { size: 10 }, color: '#999' },
      },
      y: {
        grid: { color: '#f0f0f0' },
        title: { display: true, text: 'Frequency' },
        ticks: { font: { size: 11 }, color: '#999' },
      },
    },
  };

  private createHistogram(
    values: number[],
    numBins: number,
  ): { label: string; count: number; center: number }[] {
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const binWidth = range / numBins;

    const bins = Array.from({ length: numBins }, (_, i) => ({
      label: (min + binWidth * i + binWidth / 2).toFixed(2),
      count: 0,
      center: min + binWidth * i + binWidth / 2,
    }));

    for (const v of values) {
      let idx = Math.floor((v - min) / binWidth);
      if (idx >= numBins) idx = numBins - 1;
      bins[idx].count++;
    }

    return bins;
  }
}
