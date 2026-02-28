import {
  Component,
  input,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { SignalEngineResult, SignalBacktestResult } from '../../../services/research.service';
import { TagModule } from 'primeng/tag';
import { TableModule } from 'primeng/table';
import { TooltipModule } from 'primeng/tooltip';
import { AccordionModule } from 'primeng/accordion';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

@Component({
  selector: 'app-signal-report',
  standalone: true,
  imports: [CommonModule, TagModule, TableModule, TooltipModule, AccordionModule],
  templateUrl: './signal-report.component.html',
  styleUrls: ['./signal-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalReportComponent {
  result = input.required<SignalEngineResult>();

  oosEquityCurveCanvas = viewChild<ElementRef<HTMLCanvasElement>>('oosEquityCurve');
  bestEquityCurveCanvas = viewChild<ElementRef<HTMLCanvasElement>>('bestEquityCurve');
  stabilityChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('stabilityChart');
  lifespanChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('lifespanChart');

  private oosChart: Chart | null = null;
  private bestChart: Chart | null = null;
  private stabilityChart: Chart | null = null;
  private lifespanChart: Chart | null = null;

  constructor() {
    effect(() => {
      const res = this.result();
      const oosCanvas = this.oosEquityCurveCanvas();
      const bestCanvas = this.bestEquityCurveCanvas();
      const stabCanvas = this.stabilityChartCanvas();
      const lifeCanvas = this.lifespanChartCanvas();

      if (res) {
        if (oosCanvas && res.walkForward?.combinedOosDates?.length) {
          this.renderOosEquityCurve(oosCanvas.nativeElement, res);
        }
        if (bestCanvas) {
          this.renderBestEquityCurve(bestCanvas.nativeElement, res);
        }
        if (stabCanvas && res.graduation?.parameterStability) {
          this.renderStabilityChart(stabCanvas.nativeElement, res);
        }
        if (lifeCanvas && res.walkForward?.windows?.length) {
          this.renderLifespanChart(lifeCanvas.nativeElement, res);
        }
      }
    });
  }

  // ─── Graduation ──────────────────────────────────────────

  get gradeColor(): string {
    const grade = this.result().graduation?.overallGrade ?? 'F';
    switch (grade) {
      case 'A': return '#16a34a';
      case 'B': return '#2563eb';
      case 'C': return '#d97706';
      case 'D': return '#ea580c';
      default: return '#dc2626';
    }
  }

  get statusSeverity(): 'success' | 'info' | 'warn' | 'danger' {
    const label = this.result().graduation?.statusLabel ?? 'Exploratory';
    switch (label) {
      case 'Robust Alpha': return 'success';
      case 'Conditional Alpha': return 'info';
      case 'Degrading': return 'danger';
      default: return 'warn';
    }
  }

  criterionSeverity(passed: boolean): 'success' | 'danger' {
    return passed ? 'success' : 'danger';
  }

  // ─── Backtest Grid ──────────────────────────────────────

  get uniqueThresholds(): number[] {
    const set = new Set(this.result().backtestGrid.map(r => r.threshold));
    return [...set].sort((a, b) => a - b);
  }

  get uniqueCosts(): number[] {
    const set = new Set(this.result().backtestGrid.map(r => r.costBps));
    return [...set].sort((a, b) => a - b);
  }

  getGridResult(threshold: number, cost: number): SignalBacktestResult | undefined {
    return this.result().backtestGrid.find(
      r => r.threshold === threshold && r.costBps === cost
    );
  }

  sharpeCellClass(sharpe: number): string {
    if (sharpe >= 1.0) return 'cell-green';
    if (sharpe >= 0.5) return 'cell-yellow';
    return 'cell-red';
  }

  turnoverCellClass(turnover: number): string {
    if (turnover <= 5) return 'cell-green';
    if (turnover <= 20) return 'cell-yellow';
    return 'cell-red';
  }

  // ─── Regime Coverage Grid ──────────────────────────────

  get volRegimes(): string[] {
    return ['Low Vol', 'Normal Vol', 'High Vol'];
  }

  get trendRegimes(): string[] {
    return ['Trending Up', 'Sideways', 'Trending Down'];
  }

  getRegimeCount(regime: string): number {
    const entry = this.result().regimeCoverage.find(e => e.regime === regime);
    return entry?.count ?? 0;
  }

  regimeCovered(regime: string): boolean {
    return this.getRegimeCount(regime) > 0;
  }

  // ─── Execution Assumptions ──────────────────────────────

  get executionAssumptions(): { label: string; value: string }[] {
    const r = this.result();
    return [
      { label: 'Signal Timestamp', value: 'Bar close' },
      { label: 'Execution', value: 'Next bar open' },
      { label: 'Return Measurement', value: 'Close-to-close 15m forward log return' },
      { label: 'Transaction Cost Model', value: `Fixed ${r.bestCostBps}bps per turnover` },
      { label: 'Position Sizing', value: 'Binary (sign only), max |w| = 1' },
      { label: 'Max Leverage', value: '1x' },
      { label: 'Slippage Model', value: 'Not modeled (limitation)' },
    ];
  }

  // ─── Best Config ────────────────────────────────────────

  get bestBacktest(): SignalBacktestResult | undefined {
    return this.result().backtestGrid.find(
      r => r.threshold === this.result().bestThreshold &&
           r.costBps === this.result().bestCostBps
    );
  }

  // ─── Alpha Decay ────────────────────────────────────────

  get hasAlphaDecay(): boolean {
    return (this.result().walkForward?.oosSharpeTrendSlope ?? 0) < -0.1;
  }

  // ─── Charts ─────────────────────────────────────────────

  private renderOosEquityCurve(canvas: HTMLCanvasElement, res: SignalEngineResult): void {
    if (this.oosChart) this.oosChart.destroy();
    const wf = res.walkForward;
    if (!wf) return;

    this.oosChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: wf.combinedOosDates,
        datasets: [{
          label: 'Combined OOS Cumulative Return',
          data: wf.combinedOosCumulativeReturns,
          borderColor: '#10b981',
          backgroundColor: 'rgba(16, 185, 129, 0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 1,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: 'Walk-Forward OOS Equity Curve', font: { size: 15, weight: 'bold' }, color: '#1e293b' },
          legend: { display: false },
        },
        scales: {
          y: { title: { display: true, text: 'Cumulative Return' }, grid: { color: '#f1f5f9' } },
          x: { ticks: { maxTicksLimit: 10, maxRotation: 45 }, grid: { display: false } },
        },
      },
    });
  }

  private renderBestEquityCurve(canvas: HTMLCanvasElement, res: SignalEngineResult): void {
    if (this.bestChart) this.bestChart.destroy();
    const best = this.bestBacktest;
    if (!best?.dates?.length) return;

    this.bestChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: best.dates,
        datasets: [
          {
            label: 'Cumulative Return',
            data: best.cumulativeReturns,
            borderColor: '#3b82f6',
            backgroundColor: 'rgba(59, 130, 246, 0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 1,
            borderWidth: 2,
            yAxisID: 'y',
          },
          {
            label: 'Position',
            data: best.positions,
            borderColor: '#f97316',
            borderDash: [4, 2],
            pointRadius: 0,
            borderWidth: 1.5,
            yAxisID: 'y1',
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: {
            display: true,
            text: `Best Config Equity Curve (threshold=${best.threshold}, cost=${best.costBps}bps)`,
            font: { size: 15, weight: 'bold' },
            color: '#1e293b',
          },
          legend: { position: 'bottom', labels: { usePointStyle: true } },
        },
        scales: {
          y: { position: 'left', title: { display: true, text: 'Cumulative Return' }, grid: { color: '#f1f5f9' } },
          y1: { position: 'right', title: { display: true, text: 'Position' }, min: -1.5, max: 1.5, grid: { display: false } },
          x: { ticks: { maxTicksLimit: 10, maxRotation: 45 }, grid: { display: false } },
        },
      },
    });
  }

  private renderStabilityChart(canvas: HTMLCanvasElement, res: SignalEngineResult): void {
    if (this.stabilityChart) this.stabilityChart.destroy();
    const ps = res.graduation?.parameterStability;
    if (!ps?.sharpeValuesByThreshold?.length) return;

    const sorted = [...ps.sharpeValuesByThreshold].sort((a, b) => a.threshold - b.threshold);

    this.stabilityChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: sorted.map(e => e.threshold.toFixed(1)),
        datasets: [{
          label: 'Net Sharpe',
          data: sorted.map(e => e.sharpe),
          borderColor: '#8b5cf6',
          backgroundColor: 'rgba(139, 92, 246, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 5,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: 'Parameter Stability: Net Sharpe vs Threshold', font: { size: 15, weight: 'bold' }, color: '#1e293b' },
          legend: { display: false },
        },
        scales: {
          y: { title: { display: true, text: 'Net Sharpe' }, grid: { color: '#f1f5f9' } },
          x: { title: { display: true, text: 'Threshold' }, grid: { display: false } },
        },
      },
    });
  }

  private renderLifespanChart(canvas: HTMLCanvasElement, res: SignalEngineResult): void {
    if (this.lifespanChart) this.lifespanChart.destroy();
    const windows = res.walkForward?.windows;
    if (!windows?.length) return;

    this.lifespanChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: windows.map(w => `Fold ${w.foldIndex}`),
        datasets: [{
          label: 'OOS Net Sharpe',
          data: windows.map(w => w.oosNetSharpe),
          backgroundColor: windows.map(w =>
            w.oosNetSharpe >= 0 ? 'rgba(22, 163, 74, 0.75)' : 'rgba(220, 38, 38, 0.75)'
          ),
          borderColor: windows.map(w =>
            w.oosNetSharpe >= 0 ? '#16a34a' : '#dc2626'
          ),
          borderWidth: 2,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: 'Signal Lifespan: OOS Sharpe per Fold', font: { size: 15, weight: 'bold' }, color: '#1e293b' },
          legend: { display: false },
        },
        scales: {
          y: { title: { display: true, text: 'OOS Net Sharpe' }, grid: { color: '#f1f5f9' } },
          x: { grid: { display: false } },
        },
      },
    });
  }
}
