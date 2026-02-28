import {
  Component,
  input,
  signal,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Robustness, StructuralBreakPoint } from '../../../services/research.service';
import { TagModule } from 'primeng/tag';
import { TableModule } from 'primeng/table';
import { TooltipModule } from 'primeng/tooltip';
import { Select } from 'primeng/select';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

@Component({
  selector: 'app-robustness-report',
  standalone: true,
  imports: [CommonModule, FormsModule, TagModule, TableModule, TooltipModule, Select],
  templateUrl: './robustness-report.component.html',
  styleUrls: ['./robustness-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RobustnessReportComponent {
  robustness = input.required<Robustness>();

  rollingTStatCanvas = viewChild<ElementRef<HTMLCanvasElement>>('rollingTStatChart');
  cumulativeMonthlyIcCanvas = viewChild<ElementRef<HTMLCanvasElement>>('cumulativeMonthlyIcChart');

  private rollingChart: Chart | null = null;
  private cumulativeMonthlyChart: Chart | null = null;

  // Stability method dropdown
  stabilityMethodOptions = [
    { label: '% Sign-Consistent (Recommended)', value: 'sign_consistent' },
    { label: '% Positive Months', value: 'positive' },
  ];
  selectedStabilityMethod = signal<string>('sign_consistent');

  get displayedStabilityPct(): number {
    return this.selectedStabilityMethod() === 'sign_consistent'
      ? this.robustness().pctSignConsistentMonths
      : this.robustness().pctPositiveMonths;
  }

  get displayedStabilityLabel(): string {
    return this.selectedStabilityMethod() === 'sign_consistent'
      ? this.robustness().signConsistentStabilityLabel
      : this.robustness().stabilityLabel;
  }

  get stabilityMetricSub(): string {
    return this.selectedStabilityMethod() === 'sign_consistent'
      ? 'months sign-consistent'
      : 'months positive';
  }

  get stabilityTooltip(): string {
    return this.selectedStabilityMethod() === 'sign_consistent'
      ? 'Measures % of months where IC sign matches expected direction (from train split). More robust than raw positivity.'
      : 'Based on % of months with positive IC. >70% Strong, 60-70% Tradeable, 50-60% Weak, <50% Noise';
  }

  get stabilityLabelSeverity(): 'success' | 'warn' | 'danger' | 'info' {
    const label = this.displayedStabilityLabel;
    if (label === 'Strong') return 'success';
    if (label === 'Tradeable') return 'success';
    if (label === 'Weak') return 'warn';
    if (label === 'Suspicious') return 'info';
    return 'danger';
  }

  get hasOosRetention(): boolean {
    return (this.robustness().trainTest?.oosRetention ?? 0) > 0;
  }

  get oosRetentionSeverity(): 'success' | 'warn' | 'danger' {
    const r = this.robustness().trainTest?.oosRetention ?? 0;
    if (r >= 0.6) return 'success';
    if (r >= 0.4) return 'warn';
    return 'danger';
  }

  get hasStructuralBreaks(): boolean {
    return (this.robustness().structuralBreaks?.length ?? 0) > 0;
  }

  get significantBreaks(): StructuralBreakPoint[] {
    return this.robustness().structuralBreaks?.filter(b => b.significant) ?? [];
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

    effect(() => {
      const rob = this.robustness();
      const canvas = this.cumulativeMonthlyIcCanvas();
      if (rob && canvas && rob.monthlyBreakdown.length > 0) {
        this.renderCumulativeMonthlyIcChart(canvas.nativeElement, rob);
      }
    });
  }

  abs(value: number): number {
    return Math.abs(value);
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

  private renderCumulativeMonthlyIcChart(canvas: HTMLCanvasElement, rob: Robustness): void {
    if (this.cumulativeMonthlyChart) this.cumulativeMonthlyChart.destroy();

    const months = rob.monthlyBreakdown.map(m => m.month);
    const cumIc: number[] = [];
    let sum = 0;
    for (const m of rob.monthlyBreakdown) {
      sum += m.meanIC;
      cumIc.push(sum);
    }

    // Color the line based on final direction
    const lineColor = sum >= 0 ? '#10b981' : '#ef4444';
    const bgColor = sum >= 0 ? 'rgba(16, 185, 129, 0.08)' : 'rgba(239, 68, 68, 0.08)';

    // Mark train/test split if available
    const datasets: Chart['data']['datasets'] = [
      {
        label: 'Cumulative Monthly IC',
        data: cumIc,
        borderColor: lineColor,
        backgroundColor: bgColor,
        fill: true,
        tension: 0.3,
        pointRadius: 4,
        pointHoverRadius: 7,
        borderWidth: 2,
      },
      {
        label: 'Zero',
        data: months.map(() => 0),
        borderColor: '#cbd5e1',
        borderDash: [3, 3],
        pointRadius: 0,
        borderWidth: 1,
      },
    ];

    this.cumulativeMonthlyChart = new Chart(canvas, {
      type: 'line',
      data: { labels: months, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: {
            display: true,
            text: 'Cumulative Monthly IC',
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
              filter: (item) => item.text !== 'Zero',
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
                const monthData = rob.monthlyBreakdown[ctx.dataIndex];
                return [
                  `Cumulative IC: ${Number(ctx.raw).toFixed(4)}`,
                  `Month IC: ${monthData.meanIC.toFixed(4)}`,
                ];
              },
            },
          },
        },
        scales: {
          y: {
            title: {
              display: true,
              text: 'Cumulative IC (sum of monthly means)',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: { font: { size: 12 }, color: '#64748b' },
            grid: { color: '#f1f5f9' },
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
            grid: { display: false },
          },
        },
      },
    });
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
