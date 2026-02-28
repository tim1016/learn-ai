import {
  Component,
  input,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { Robustness } from '../../../services/research.service';
import { TagModule } from 'primeng/tag';
import { TableModule } from 'primeng/table';
import { TooltipModule } from 'primeng/tooltip';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

@Component({
  selector: 'app-robustness-report',
  standalone: true,
  imports: [CommonModule, TagModule, TableModule, TooltipModule],
  templateUrl: './robustness-report.component.html',
  styleUrls: ['./robustness-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RobustnessReportComponent {
  robustness = input.required<Robustness>();

  rollingTStatCanvas = viewChild<ElementRef<HTMLCanvasElement>>('rollingTStatChart');

  private rollingChart: Chart | null = null;

  get stabilityLabelSeverity(): 'success' | 'warn' | 'danger' | 'info' {
    const label = this.robustness().stabilityLabel;
    if (label === 'Strong') return 'success';
    if (label === 'Tradeable') return 'success';
    if (label === 'Weak') return 'warn';
    if (label === 'Suspicious') return 'info';
    return 'danger';
  }

  get hasMonthlyData(): boolean {
    return this.robustness().monthlyBreakdown.length > 0;
  }

  get hasRollingData(): boolean {
    return this.robustness().rollingTStat.length > 0;
  }

  get hasVolRegimes(): boolean {
    return this.robustness().volatilityRegimes.length > 0;
  }

  get hasTrendRegimes(): boolean {
    return this.robustness().trendRegimes.length > 0;
  }

  get hasTrainTest(): boolean {
    return this.robustness().trainTest != null;
  }

  get overfitSeverity(): 'success' | 'danger' {
    return this.robustness().trainTest?.overfitFlag ? 'danger' : 'success';
  }

  get overfitLabel(): string {
    return this.robustness().trainTest?.overfitFlag ? 'OVERFIT WARNING' : 'CONSISTENT';
  }

  get trainTestDelta(): number {
    const tt = this.robustness().trainTest;
    if (!tt) return 0;
    return tt.trainMeanIC - tt.testMeanIC;
  }

  constructor() {
    effect(() => {
      const rob = this.robustness();
      const canvas = this.rollingTStatCanvas();
      if (rob && canvas && rob.rollingTStat.length > 0) {
        this.renderRollingTStatChart(canvas.nativeElement, rob);
      }
    });
  }

  monthlyIcSeverity(meanIc: number): string {
    if (meanIc >= 0.03) return 'text-green-700';
    if (meanIc > 0) return 'text-green-600';
    return 'text-red-700';
  }

  monthlyTStatSeverity(tStat: number): string {
    return Math.abs(tStat) >= 1.65 ? 'text-green-700' : 'text-gray-500';
  }

  regimeIcClass(meanIc: number): string {
    if (Math.abs(meanIc) >= 0.03) return 'text-green-700 font-semibold';
    if (meanIc > 0) return 'text-green-600';
    return 'text-red-700';
  }

  private renderRollingTStatChart(canvas: HTMLCanvasElement, rob: Robustness): void {
    if (this.rollingChart) this.rollingChart.destroy();

    const months = rob.rollingTStat.map(r => r.month);
    const values = rob.rollingTStat.map(r => r.tStatSmoothed);
    const sigLine = months.map(() => 1.65);
    const zeroLine = months.map(() => 0);

    this.rollingChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: months,
        datasets: [
          {
            label: '6-Month Smoothed t-Stat',
            data: values,
            borderColor: '#8b5cf6',
            backgroundColor: 'rgba(139, 92, 246, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 4,
            pointHoverRadius: 7,
            borderWidth: 2,
          },
          {
            label: 'Significance (t = 1.65)',
            data: sigLine,
            borderColor: '#f97316',
            borderDash: [6, 4],
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: 'Zero',
            data: zeroLine,
            borderColor: '#cbd5e1',
            borderDash: [3, 3],
            pointRadius: 0,
            borderWidth: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: {
            display: true,
            text: 'Rolling 6-Month Smoothed t-Statistic',
            font: { size: 15, weight: 'bold' },
            color: '#1e293b',
            padding: { bottom: 16 },
          },
          legend: {
            position: 'bottom',
            labels: {
              font: { size: 12 },
              color: '#475569',
              padding: 16,
              usePointStyle: true,
            },
          },
          tooltip: {
            backgroundColor: '#1e293b',
            titleFont: { size: 13 },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 6,
            callbacks: {
              label: (ctx) => {
                if (ctx.dataset.label === 'Zero') return '';
                return `${ctx.dataset.label}: ${Number(ctx.raw).toFixed(2)}`;
              },
            },
          },
        },
        scales: {
          y: {
            title: {
              display: true,
              text: 't-Statistic',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: {
              font: { size: 12 },
              color: '#64748b',
            },
            grid: {
              color: '#f1f5f9',
            },
          },
          x: {
            title: {
              display: true,
              text: 'Month',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: {
              font: { size: 11 },
              color: '#64748b',
              maxRotation: 45,
              maxTicksLimit: 12,
            },
            grid: {
              display: false,
            },
          },
        },
      },
    });
  }
}
