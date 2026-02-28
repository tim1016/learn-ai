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
import { ResearchResult } from '../../../services/research.service';
import { RobustnessReportComponent } from '../robustness-report/robustness-report.component';
import { TagModule } from 'primeng/tag';
import { TableModule } from 'primeng/table';
import { TooltipModule } from 'primeng/tooltip';
import { Select } from 'primeng/select';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

@Component({
  selector: 'app-feature-report',
  standalone: true,
  imports: [CommonModule, FormsModule, TagModule, TableModule, TooltipModule, Select, RobustnessReportComponent],
  templateUrl: './feature-report.component.html',
  styleUrls: ['./feature-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeatureReportComponent {
  result = input.required<ResearchResult>();

  icChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('icChart');
  quantileChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('quantileChart');
  cumulativeIcCanvas = viewChild<ElementRef<HTMLCanvasElement>>('cumulativeIcChart');

  private icChart: Chart | null = null;
  private quantileChart: Chart | null = null;
  private cumulativeIcChart: Chart | null = null;

  // t-Stat method dropdown
  tStatOptions = [
    { label: 'Newey-West t-Stat (Recommended)', value: 'nw' },
    { label: 'Standard t-Stat', value: 'standard' },
  ];
  selectedTStatMethod = signal<string>('nw');

  get validationSeverity(): 'success' | 'danger' {
    return this.result().passedValidation ? 'success' : 'danger';
  }

  get validationLabel(): string {
    return this.result().passedValidation ? 'VALIDATED' : 'NOT VALIDATED';
  }

  get validationVerdict(): string {
    const r = this.result();
    if (r.passedValidation) {
      const direction = r.meanIC < 0
        ? 'Negative IC indicates a mean-reversion (contrarian) effect — higher feature values predict lower forward returns.'
        : 'Positive IC indicates a momentum effect — higher feature values predict higher forward returns.';
      return `${r.featureName} demonstrates statistically significant predictive power for ${r.ticker}. ` +
        `Mean IC of ${r.meanIC.toFixed(4)} with ${this.tStatLabel} ${this.displayedTStat.toFixed(2)} (p=${this.displayedPValue.toFixed(4)}). ${direction}`;
    }
    const issues: string[] = [];
    if (Math.abs(r.meanIC) < 0.03) issues.push('weak IC magnitude');
    if (Math.abs(this.displayedTStat) < 1.96) issues.push('insufficient statistical significance');
    if (!r.isStationary) issues.push('non-stationary feature');
    if (!r.isMonotonic) issues.push('non-monotonic quantile returns');
    return `${r.featureName} did not pass validation for ${r.ticker}: ${issues.join(', ')}.`;
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

  get icSignalStrength(): string {
    const ic = Math.abs(this.result().meanIC);
    if (ic >= 0.1) return 'Strong';
    if (ic >= 0.05) return 'Moderate';
    if (ic >= 0.03) return 'Weak but usable';
    return 'Negligible';
  }

  get displayedTStat(): number {
    return this.selectedTStatMethod() === 'nw'
      ? this.result().nwTStat
      : this.result().icTStat;
  }

  get displayedPValue(): number {
    return this.selectedTStatMethod() === 'nw'
      ? this.result().nwPValue
      : this.result().icPValue;
  }

  get tStatLabel(): string {
    return this.selectedTStatMethod() === 'nw' ? 'NW t-Stat' : 'IC t-Stat';
  }

  get isTStatSignificant(): boolean {
    return Math.abs(this.displayedTStat) >= 1.96;
  }

  get tStatSignificanceLabel(): string {
    const absT = Math.abs(this.displayedTStat);
    if (absT >= 2.576) return 'Highly significant';
    if (absT >= 1.96) return 'Significant';
    if (absT >= 1.645) return 'Weakly significant';
    return 'Not significant';
  }

  get pValueSignificanceLabel(): string {
    const p = this.displayedPValue;
    if (p < 0.01) return 'p < 0.01';
    if (p < 0.05) return 'p < 0.05';
    if (p < 0.10) return 'p < 0.10';
    return 'Not significant';
  }

  get isPValueSignificant(): boolean {
    return this.displayedPValue < 0.05;
  }

  get isIcMeaningful(): boolean {
    return Math.abs(this.result().meanIC) >= 0.03;
  }

  get icDirectionLabel(): string {
    return this.result().meanIC < 0 ? 'Mean-reversion' : 'Momentum';
  }

  get tStatTooltip(): string {
    return this.selectedTStatMethod() === 'nw'
      ? 'Newey-West HAC-corrected t-stat accounts for serial correlation in daily ICs. More conservative and reliable than standard t-stat.'
      : 'Statistical significance of mean IC. t > 1.65 rejects H0 at 10% level (meaningful signal).';
  }

  get pValueTooltip(): string {
    return this.selectedTStatMethod() === 'nw'
      ? 'p-value from the Newey-West corrected t-stat. Accounts for autocorrelation in the IC series.'
      : 'Probability of observing this IC by chance. p < 0.10 confirms significance.';
  }

  constructor() {
    effect(() => {
      const res = this.result();
      const icCanvas = this.icChartCanvas();
      const qCanvas = this.quantileChartCanvas();
      const cumCanvas = this.cumulativeIcCanvas();
      if (res && icCanvas && qCanvas) {
        this.renderIcChart(icCanvas.nativeElement, res);
        this.renderQuantileChart(qCanvas.nativeElement, res);
        if (cumCanvas) {
          this.renderCumulativeIcChart(cumCanvas.nativeElement, res);
        }
      }
    });
  }

  private renderIcChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.icChart) this.icChart.destroy();

    const meanLine = res.icDates.map(() => res.meanIC);
    const zeroLine = res.icDates.map(() => 0);

    this.icChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: res.icDates,
        datasets: [
          {
            label: 'Daily IC',
            data: res.icValues,
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59, 130, 246, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 3,
            pointHoverRadius: 6,
            borderWidth: 2,
          },
          {
            label: `Mean IC (${res.meanIC.toFixed(4)})`,
            data: meanLine,
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
            text: 'Rolling Information Coefficient (Daily Spearman \u03C1)',
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
                return `${ctx.dataset.label}: ${Number(ctx.raw).toFixed(4)}`;
              },
            },
          },
        },
        scales: {
          y: {
            title: {
              display: true,
              text: 'IC (Spearman \u03C1)',
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
              text: 'Date',
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

  private renderQuantileChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.quantileChart) this.quantileChart.destroy();

    const bins = res.quantileBins;
    const colors = bins.map(b =>
      b.meanReturn >= 0 ? 'rgba(22, 163, 74, 0.75)' : 'rgba(220, 38, 38, 0.75)'
    );
    const borderColors = bins.map(b =>
      b.meanReturn >= 0 ? '#16a34a' : '#dc2626'
    );

    this.quantileChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: bins.map(b => `Q${b.binNumber}`),
        datasets: [
          {
            label: 'Mean Forward Return',
            data: bins.map(b => b.meanReturn),
            backgroundColor: colors,
            borderColor: borderColors,
            borderWidth: 2,
            borderRadius: 4,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: {
            display: true,
            text: 'Quantile Mean Returns \u2014 E[R|Q]',
            font: { size: 15, weight: 'bold' },
            color: '#1e293b',
            padding: { bottom: 16 },
          },
          legend: {
            display: false,
          },
          tooltip: {
            backgroundColor: '#1e293b',
            titleFont: { size: 13 },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 6,
            callbacks: {
              label: (ctx) => {
                const bin = bins[ctx.dataIndex];
                return [
                  `Mean Return: ${bin.meanReturn.toFixed(6)}`,
                  `Range: [${bin.lowerBound.toFixed(4)}, ${bin.upperBound.toFixed(4)}]`,
                  `Samples: ${bin.count}`,
                ];
              },
            },
          },
        },
        scales: {
          y: {
            title: {
              display: true,
              text: 'Mean Log Return',
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
              text: 'Feature Quantile (Low \u2192 High)',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: {
              font: { size: 13, weight: 'bold' },
              color: '#334155',
            },
            grid: {
              display: false,
            },
          },
        },
      },
    });
  }

  private renderCumulativeIcChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.cumulativeIcChart) this.cumulativeIcChart.destroy();

    const cumIc: number[] = [];
    let sum = 0;
    for (const ic of res.icValues) {
      sum += ic;
      cumIc.push(sum);
    }

    this.cumulativeIcChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: res.icDates,
        datasets: [
          {
            label: 'Cumulative IC',
            data: cumIc,
            borderColor: '#10b981',
            backgroundColor: 'rgba(16, 185, 129, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 2,
            pointHoverRadius: 5,
            borderWidth: 2,
          },
          {
            label: 'Zero',
            data: res.icDates.map(() => 0),
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
            text: 'Cumulative Information Coefficient (\u03A3 IC)',
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
                return `Cumulative IC: ${Number(ctx.raw).toFixed(4)}`;
              },
            },
          },
        },
        scales: {
          y: {
            title: {
              display: true,
              text: 'Cumulative IC',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: { font: { size: 12 }, color: '#64748b' },
            grid: { color: '#f1f5f9' },
          },
          x: {
            title: {
              display: true,
              text: 'Date',
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
}
