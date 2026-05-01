import {
  Component,
  input,
  signal,
  computed,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { RouterModule } from '@angular/router';
import {
  SignalEngineResult,
  SignalBacktestResult,
  RegimeBucket,
} from '../../../services/research.service';
import { TagModule } from 'primeng/tag';
import { TableModule } from 'primeng/table';
import { TooltipModule } from 'primeng/tooltip';
import { AccordionModule } from 'primeng/accordion';
import { ButtonModule } from 'primeng/button';
import { Chart, registerables } from 'chart.js';
import { SignalVerdictBlockComponent } from './signal-verdict-block/signal-verdict-block.component';

Chart.register(...registerables);

@Component({
  selector: 'app-signal-report',
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    TagModule,
    TableModule,
    TooltipModule,
    AccordionModule,
    ButtonModule,
    SignalVerdictBlockComponent,
  ],
  templateUrl: './signal-report.component.html',
  styleUrls: ['./signal-report.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalReportComponent {
  result = input.required<SignalEngineResult>();

  /** Stage 0 collapse — when the kill switch fires, downstream panels are
   *  hidden behind a "Show diagnostic details anyway" disclosure so the
   *  reader is not seduced into reading meaning into noise. */
  readonly showDetailsAnyway = signal(false);

  readonly graduationStage = computed<0 | 1 | 2 | 3>(() => {
    return (this.result().graduation?.stageInfo?.stage ?? 0) as 0 | 1 | 2 | 3;
  });

  readonly isStage0Rejected = computed<boolean>(() => this.graduationStage() === 0);

  readonly showDownstreamPanels = computed<boolean>(
    () => !this.isStage0Rejected() || this.showDetailsAnyway(),
  );

  toggleDetailsAnyway(): void {
    this.showDetailsAnyway.update((v) => !v);
  }

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

  /** Execution model as actually implemented in the backtest kernel. The
   *  text was rewritten in this redesign — the previous "Next bar open"
   *  label did not match what the code computes (1-bar lag with
   *  close-to-close measurement). See ``docs/signal-engine-authority.md``
   *  § 6 for the full timing model and the realism caveats. */
  get executionAssumptions(): { label: string; value: string; note?: string }[] {
    const r = this.result();
    return [
      { label: 'Signal computed', value: 'At bar close of t−1' },
      { label: 'Position effective', value: 'From bar t onward (1-bar lag)' },
      {
        label: 'Return measurement',
        value: 'close_t → close_{t+15} (15-bar log return)',
        note: 'No look-ahead: position[t-1] · return[t]. Equivalent to "filled at next bar close".',
      },
      { label: 'Position sizing', value: 'Binary (±1 / 0), max |w| = 1' },
      { label: 'Max leverage', value: '1×' },
      {
        label: 'Transaction cost',
        value: `Fixed ${r.bestCostBps}bps per side on |Δw|`,
        note: 'Cost grid (1–5 bps) is optimistic at high turnover. See § 6 of the methodology authority.',
      },
      {
        label: 'Slippage / market impact',
        value: 'Not modelled',
        note: 'Realistic at low turnover; materially understated above 200×/yr turnover.',
      },
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

  /** True when the alpha-decay regression had enough folds (≥ 5) to be
   *  statistically meaningful. Below that, the UI must render an
   *  "insufficient folds" placeholder rather than a misleading p-value. */
  get alphaDecayTestValid(): boolean {
    return this.result().walkForward?.alphaDecay?.isTestValid ?? false;
  }

  get alphaDecayFoldCount(): number {
    return this.result().walkForward?.alphaDecay?.nFoldsUsed ?? 0;
  }

  // ─── Joint regime coverage ───────────────────────────────

  get jointRegimeBuckets(): RegimeBucket[] {
    return this.result().jointRegimeCoverage ?? [];
  }

  /** Look up the joint bucket for a (vol, trend) pair. Returns ``null`` when
   *  the pair has zero observations — the cell will render as Empty. */
  jointBucket(vol: string, trend: string): RegimeBucket | null {
    return (
      this.jointRegimeBuckets.find(
        (b) => b.volLabel === vol && b.trendLabel === trend,
      ) ?? null
    );
  }

  /** Visual band for a joint cell — drives the colored left border / chip. */
  jointBucketBand(bucket: RegimeBucket | null): 'green' | 'amber' | 'red' | 'na' {
    if (!bucket || bucket.days === 0) return 'na';
    if (bucket.badge === 'Pass') return 'green';
    if (bucket.badge === 'Sparse') return 'amber';
    return 'red';
  }

  // ─── Deflated Sharpe ────────────────────────────────────

  get deflatedSharpeBand(): 'green' | 'amber' | 'red' | 'na' {
    const dsr = this.result().deflatedSharpe;
    if (!dsr?.valid) return 'na';
    if (dsr.dsrProbability >= 0.95) return 'green';
    if (dsr.dsrProbability >= 0.5) return 'amber';
    return 'red';
  }

  // ─── Executive Summary ────────────────────────────────────

  get executiveSummary(): string {
    const r = this.result();
    const grad = r.graduation;
    if (!grad) return '';

    const parts: string[] = [];
    parts.push(`Signal "${r.featureName}" on ${r.ticker} received grade ${grad.overallGrade} (${grad.statusLabel}).`);

    if (r.walkForward?.windows?.length) {
      const wf = r.walkForward;
      parts.push(`Mean OOS Sharpe is ${wf.meanOosSharpe.toFixed(2)} across ${wf.windows.length} folds.`);
    }

    if (this.alphaDecaySignificant) {
      parts.push('Alpha decay is statistically significant (p < 0.05) — signal edge may be eroding over time.');
    } else if (this.hasAlphaDecay) {
      parts.push('Negative Sharpe trend detected but not statistically significant.');
    }

    return parts.join(' ');
  }

  get oosSharpeDivergence(): number {
    const wf = this.result().walkForward;
    if (!wf) return 0;
    return Math.abs(wf.meanOosSharpe - wf.medianOosSharpe);
  }

  get hasSharpeDivergence(): boolean {
    return this.oosSharpeDivergence > 0.15;
  }

  get alphaDecaySignificant(): boolean {
    return (this.result().walkForward?.alphaDecay?.pValue ?? 1) < 0.05;
  }

  get skewnessInterpretation(): string {
    const skew = this.result().signalBehavior?.skewnessActiveReturns ?? 0;
    if (skew > 0.5) return 'Positive skew — right tail dominates, favorable for trend strategies.';
    if (skew < -0.5) return 'Negative skew — left tail dominates, risk of large drawdowns.';
    return 'Approximately symmetric return distribution.';
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

  private renderBestEquityCurve(canvas: HTMLCanvasElement, _res: SignalEngineResult): void {
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
