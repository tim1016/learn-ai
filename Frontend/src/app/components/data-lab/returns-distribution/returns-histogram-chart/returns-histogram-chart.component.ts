import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnChanges,
  OnDestroy,
  output,
  input,
  viewChild,
} from '@angular/core';
import { Chart, registerables } from 'chart.js';

import { KindDistribution } from '../returns-distribution.service';

Chart.register(...registerables);

/** Closed copy map: bin labels are presentation, authored here. */
function binLabel(lowerPct: number | null, upperPct: number | null): string {
  if (lowerPct === null) return `< ${upperPct!.toFixed(1)}%`;
  if (upperPct === null) return `≥ ${lowerPct.toFixed(1)}%`;
  return `${lowerPct.toFixed(1)}…${upperPct.toFixed(1)}%`;
}

/**
 * The daily-returns histogram: one bar per bin (open edge bins visually
 * distinct) with the Python-computed normal overlay as a line. Purely
 * presentational — every count and overlay point arrives computed; the only
 * arithmetic here is axis labels. Clicking a bar emits its bin index.
 */
@Component({
  selector: 'app-returns-histogram-chart',
  template: `
    <div class="hist-wrap">
      <canvas #histCanvas role="img" aria-label="Daily returns histogram"></canvas>
    </div>
  `,
  styles: `
    :host { display: block; }
    .hist-wrap { position: relative; height: 340px; }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReturnsHistogramChartComponent implements OnChanges, OnDestroy {
  readonly distribution = input.required<KindDistribution>();
  readonly selectedBinIndex = input<number | null>(null);
  readonly binSelected = output<number>();

  private readonly canvas =
    viewChild.required<ElementRef<HTMLCanvasElement>>('histCanvas');
  private chart: Chart | null = null;

  ngOnChanges(): void {
    this.render();
  }

  ngOnDestroy(): void {
    this.chart?.destroy();
    this.chart = null;
  }

  private render(): void {
    const canvas = this.canvas().nativeElement;
    const dist = this.distribution();
    this.chart?.destroy();

    const labels = dist.bins.map((b) => binLabel(b.lowerPct, b.upperPct));
    const selected = this.selectedBinIndex();
    const bgColors = dist.bins.map((b, i) => {
      if (i === selected) return 'rgba(255, 152, 0, 0.85)';
      return b.isEdge ? 'rgba(171, 71, 188, 0.55)' : 'rgba(107, 111, 122, 0.55)';
    });

    this.chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {
            label: 'Days',
            data: dist.bins.map((b) => b.count),
            backgroundColor: bgColors,
            borderColor: dist.bins.map((_, i) =>
              i === selected ? '#ff9800' : '#6b6f7a',
            ),
            borderWidth: 1,
            order: 1,
          },
          {
            label: 'Normal fit',
            data: dist.normalExpectedCounts,
            type: 'line',
            borderColor: 'rgba(66, 165, 245, 0.9)',
            backgroundColor: 'rgba(66, 165, 245, 0.9)',
            pointRadius: 2,
            tension: 0.3,
            order: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        onClick: (_event, elements) => {
          if (elements.length > 0) this.binSelected.emit(elements[0].index);
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#131722',
            callbacks: {
              title: (items) => `Daily return ${items[0].label}`,
              label: (ctx) =>
                ctx.dataset.type === 'line'
                  ? ` normal fit: ${Number(ctx.raw).toFixed(1)} days`
                  : ` ${ctx.raw} days in this basket`,
            },
          },
        },
        scales: {
          y: {
            title: { display: true, text: 'Number of days', color: '#475569' },
            ticks: { precision: 0, color: '#64748b' },
            grid: { color: 'rgba(30, 34, 45, 0.8)' },
          },
          x: {
            title: { display: true, text: 'Day’s move', color: '#475569' },
            ticks: { color: '#64748b', maxRotation: 60, autoSkip: false, font: { size: 10 } },
            grid: { display: false },
          },
        },
      },
    });
  }
}
