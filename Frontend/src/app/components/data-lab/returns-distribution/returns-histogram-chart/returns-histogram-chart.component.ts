import {
  AfterViewInit,
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

/** Closed copy map: bin labels are presentation, authored here.
 * Edge bins have exactly one finite bound; the missing one is formatted
 * from the other side. */
function binLabel(lowerPct: number | null, upperPct: number | null): string {
  if (lowerPct === null) return `< ${(upperPct ?? 0).toFixed(1)}%`;
  if (upperPct === null) return `≥ ${lowerPct.toFixed(1)}%`;
  return `${lowerPct.toFixed(1)}…${upperPct.toFixed(1)}%`;
}

/**
 * The daily-returns histogram: one bar per bin (open edge bins visually
 * distinct) with the Python-computed normal overlay as a line. Purely
 * presentational — every count and overlay point arrives computed; the only
 * arithmetic here is axis labels. Clicking a bar (or focusing the canvas and
 * using the keyboard) emits its bin index.
 */
@Component({
  selector: 'app-returns-histogram-chart',
  template: `
    <div class="hist-wrap">
      <canvas
        #histCanvas
        tabindex="0"
        role="img"
        aria-label="Daily returns histogram. Use left and right arrow keys to pick a basket, Enter or Space to open or close it."
        (keydown)="onKeydown($event)"
      ></canvas>
    </div>
  `,
  styles: `
    :host { display: block; }
    .hist-wrap { position: relative; height: 340px; }
    canvas:focus-visible { outline: 2px solid #ff9800; outline-offset: 2px; }
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReturnsHistogramChartComponent implements AfterViewInit, OnChanges, OnDestroy {
  readonly distribution = input.required<KindDistribution>();
  readonly selectedBinIndex = input<number | null>(null);
  readonly binSelected = output<number>();

  private readonly canvas =
    viewChild.required<ElementRef<HTMLCanvasElement>>('histCanvas');
  private chart: Chart | null = null;

  /** The required view query resolves by ngAfterViewInit; the initial
   * ngOnChanges fires earlier, so the first render waits for the view and
   * later input changes re-render only once the chart exists. */
  ngAfterViewInit(): void {
    this.render();
  }

  ngOnChanges(): void {
    if (this.chart !== null) this.render();
  }

  ngOnDestroy(): void {
    this.chart?.destroy();
    this.chart = null;
  }

  /** Keyboard bin selection: arrows/Home/End move the selection, Enter and
   * Space toggle it — the same binSelected emission the canvas click
   * produces, so keyboard users reach the drill-down identically. */
  onKeydown(event: KeyboardEvent): void {
    const bins = this.distribution().bins;
    if (bins.length === 0) return;
    const current = this.selectedBinIndex();
    let target: number | null = null;
    switch (event.key) {
      case 'ArrowRight':
        target = Math.min((current ?? -1) + 1, bins.length - 1);
        break;
      case 'ArrowLeft':
        target = Math.max((current ?? bins.length) - 1, 0);
        break;
      case 'Home':
        target = 0;
        break;
      case 'End':
        target = bins.length - 1;
        break;
      case 'Enter':
      case ' ':
        if (current !== null) target = current;
        break;
      default:
        return;
    }
    if (target !== null) {
      event.preventDefault();
      this.binSelected.emit(target);
    }
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
