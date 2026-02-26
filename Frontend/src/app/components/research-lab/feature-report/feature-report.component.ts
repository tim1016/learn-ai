import {
  Component,
  input,
  computed,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  afterNextRender,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ResearchResult } from '../../../services/research.service';
import { TagModule } from 'primeng/tag';
import { TableModule } from 'primeng/table';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

@Component({
  selector: 'app-feature-report',
  standalone: true,
  imports: [CommonModule, TagModule, TableModule],
  templateUrl: './feature-report.component.html',
  styleUrls: ['./feature-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeatureReportComponent {
  result = input.required<ResearchResult>();

  icChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('icChart');
  quantileChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('quantileChart');

  private icChart: Chart | null = null;
  private quantileChart: Chart | null = null;

  get validationSeverity(): 'success' | 'danger' {
    return this.result().passedValidation ? 'success' : 'danger';
  }

  get validationLabel(): string {
    return this.result().passedValidation ? 'PASSED' : 'FAILED';
  }

  get stationaritySeverity(): 'success' | 'warn' {
    return this.result().isStationary ? 'success' : 'warn';
  }

  get stationarityLabel(): string {
    return this.result().isStationary ? 'Stationary' : 'Non-Stationary';
  }

  get monotonicitySeverity(): 'success' | 'warn' {
    return this.result().isMonotonic ? 'success' : 'warn';
  }

  get monotonicityLabel(): string {
    return this.result().isMonotonic ? 'Monotonic' : 'Non-Monotonic';
  }

  constructor() {
    effect(() => {
      const res = this.result();
      const icCanvas = this.icChartCanvas();
      const qCanvas = this.quantileChartCanvas();
      if (res && icCanvas && qCanvas) {
        this.renderIcChart(icCanvas.nativeElement, res);
        this.renderQuantileChart(qCanvas.nativeElement, res);
      }
    });
  }

  private renderIcChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.icChart) this.icChart.destroy();

    this.icChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: res.icDates,
        datasets: [
          {
            label: 'Daily IC',
            data: res.icValues,
            borderColor: '#60a5fa',
            backgroundColor: 'rgba(96, 165, 250, 0.1)',
            fill: true,
            tension: 0.3,
            pointRadius: 4,
          },
          {
            label: 'Mean IC',
            data: res.icDates.map(() => res.meanIC),
            borderColor: '#f97316',
            borderDash: [5, 5],
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          title: { display: true, text: 'Rolling Information Coefficient' },
        },
        scales: {
          y: { title: { display: true, text: 'IC (Spearman ρ)' } },
        },
      },
    });
  }

  private renderQuantileChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.quantileChart) this.quantileChart.destroy();

    const bins = res.quantileBins;
    this.quantileChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: bins.map(b => `Q${b.binNumber}`),
        datasets: [
          {
            label: 'Mean Return by Quantile',
            data: bins.map(b => b.meanReturn),
            backgroundColor: bins.map(b =>
              b.meanReturn >= 0 ? 'rgba(34, 197, 94, 0.7)' : 'rgba(239, 68, 68, 0.7)'
            ),
            borderColor: bins.map(b =>
              b.meanReturn >= 0 ? '#22c55e' : '#ef4444'
            ),
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        plugins: {
          title: { display: true, text: 'Quantile Mean Returns (E[R|Q])' },
        },
        scales: {
          y: { title: { display: true, text: 'Mean Log Return' } },
          x: { title: { display: true, text: 'Feature Quantile' } },
        },
      },
    });
  }
}
