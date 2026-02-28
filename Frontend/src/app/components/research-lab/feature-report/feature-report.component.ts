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
import { AccordionModule } from 'primeng/accordion';
import { Slider } from 'primeng/slider';
import { Chart, registerables } from 'chart.js';

Chart.register(...registerables);

interface GradeDimension {
  label: string;
  grade: string;
  tooltip: string;
}

interface FalsificationCriterion {
  label: string;
  description: string;
  triggered: boolean;
}

interface CostScenario {
  costBps: number;
  grossBps: number;
  netBps: number;
  viable: boolean;
}

@Component({
  selector: 'app-feature-report',
  standalone: true,
  imports: [
    CommonModule, FormsModule, TagModule, TableModule, TooltipModule,
    Select, AccordionModule, Slider, RobustnessReportComponent,
  ],
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

  // Transaction cost slider
  costBps = signal<number>(2);

  // ─── Sample Size & Coverage ─────────────────────────────

  get icDaysCount(): number {
    return this.result().icDates.length;
  }

  get requestedDays(): number {
    const start = new Date(this.result().startDate);
    const end = new Date(this.result().endDate);
    return Math.max(1, Math.ceil((end.getTime() - start.getTime()) / (1000 * 60 * 60 * 24)));
  }

  get coverageRatio(): number {
    return this.result().effectiveN / this.requestedDays;
  }

  get coverageSeverity(): 'success' | 'warn' | 'danger' {
    if (this.coverageRatio >= 0.70) return 'success';
    if (this.coverageRatio >= 0.30) return 'warn';
    return 'danger';
  }

  get effectiveNSeverity(): 'success' | 'warn' | 'danger' {
    if (this.result().effectiveN >= 250) return 'success';
    if (this.result().effectiveN >= 60) return 'warn';
    return 'danger';
  }

  // ─── Confidence Tiers ───────────────────────────────────

  get confidenceTier(): 1 | 2 | 3 | 4 {
    const days = this.icDaysCount;
    if (days >= 750) return 1;
    if (days >= 250) return 2;
    if (days >= 60) return 3;
    return 4;
  }

  get confidenceTierLabel(): string {
    switch (this.confidenceTier) {
      case 1: return 'Tier 1 — Extended Track Record';
      case 2: return 'Tier 2 — Standard Validation';
      case 3: return 'Tier 3 — Preliminary Analysis';
      case 4: return 'Tier 4 — Exploratory Only';
    }
  }

  get confidenceTierDescription(): string {
    switch (this.confidenceTier) {
      case 1: return `${this.icDaysCount} IC days (3+ years). Sufficient for regime-conditional analysis and allocation decisions.`;
      case 2: return `${this.icDaysCount} IC days (1+ year). Adequate for initial signal evaluation. Regime coverage may be incomplete.`;
      case 3: return `${this.icDaysCount} IC days (3-12 months). Preliminary results only. Statistical tests have limited power at this sample size.`;
      case 4: return `${this.icDaysCount} IC days (<3 months). Insufficient for meaningful validation. All metrics should be treated as directional estimates only.`;
    }
  }

  get confidenceTierSeverity(): 'success' | 'info' | 'warn' | 'danger' {
    switch (this.confidenceTier) {
      case 1: return 'success';
      case 2: return 'info';
      case 3: return 'warn';
      case 4: return 'danger';
    }
  }

  // ─── Self-Critical Auto-Override ────────────────────────

  get isExploratoryOverride(): boolean {
    return this.result().effectiveN < 60;
  }

  // ─── Research Grade Card ────────────────────────────────

  get researchGrade(): GradeDimension[] {
    return [
      {
        label: 'Statistical Strength',
        grade: this.computeStatisticalGrade(),
        tooltip: 'Based on |Mean IC|, NW t-stat significance, quantile monotonicity, and stationarity.',
      },
      {
        label: 'Stability',
        grade: this.computeStabilityGrade(),
        tooltip: 'Based on sign consistency across months, OOS retention, and structural break detection.',
      },
      {
        label: 'Sample Depth',
        grade: this.computeSampleDepthGrade(),
        tooltip: 'Based on Effective N and regime coverage completeness.',
      },
      {
        label: 'Execution Realism',
        grade: 'Not Modeled',
        tooltip: 'Transaction costs, slippage, and market impact are not modeled. See cost sensitivity section for rough estimates.',
      },
    ];
  }

  gradeColor(grade: string): string {
    switch (grade) {
      case 'A': return '#16a34a';
      case 'B': return '#2563eb';
      case 'C': return '#d97706';
      case 'D': return '#ea580c';
      case 'F': return '#dc2626';
      default: return '#94a3b8';
    }
  }

  private computeStatisticalGrade(): string {
    const absIC = Math.abs(this.result().meanIC);
    const absTStat = Math.abs(this.displayedTStat);

    let score = 0;
    if (absIC >= 0.10) score += 4;
    else if (absIC >= 0.05) score += 3;
    else if (absIC >= 0.03) score += 2;
    else score += 1;

    if (absTStat >= 2.576) score += 4;
    else if (absTStat >= 1.96) score += 3;
    else if (absTStat >= 1.645) score += 2;

    if (this.result().isMonotonic) score += 2;
    if (this.result().isStationary) score += 2;

    if (score >= 10) return 'A';
    if (score >= 8) return 'B';
    if (score >= 6) return 'C';
    if (score >= 4) return 'D';
    return 'F';
  }

  private computeStabilityGrade(): string {
    const rob = this.result().robustness;
    if (!rob) return 'N/A';

    let score = 0;
    const signConsistent = rob.pctSignConsistentMonths;
    if (signConsistent >= 0.70) score += 4;
    else if (signConsistent >= 0.60) score += 3;
    else if (signConsistent >= 0.50) score += 2;
    else score += 1;

    if (rob.trainTest) {
      if (rob.trainTest.oosRetention >= 0.80) score += 4;
      else if (rob.trainTest.oosRetention >= 0.60) score += 3;
      else if (rob.trainTest.oosRetention >= 0.40) score += 2;
    }

    const sigBreaks = rob.structuralBreaks?.filter(b => b.significant).length ?? 0;
    if (sigBreaks === 0) score += 2;

    if (score >= 8) return 'A';
    if (score >= 6) return 'B';
    if (score >= 4) return 'C';
    if (score >= 2) return 'D';
    return 'F';
  }

  private computeSampleDepthGrade(): string {
    let score = 0;
    if (this.result().effectiveN >= 750) score += 4;
    else if (this.result().effectiveN >= 250) score += 3;
    else if (this.result().effectiveN >= 60) score += 2;

    const regimeCount = this.regimeCoverageCount;
    if (regimeCount >= 5) score += 3;
    else if (regimeCount >= 3) score += 2;
    else score += 1;

    if (score >= 6) return 'A';
    if (score >= 5) return 'B';
    if (score >= 3) return 'C';
    if (score >= 2) return 'D';
    return 'F';
  }

  private get regimeCoverageCount(): number {
    const rob = this.result().robustness;
    if (!rob) return 0;
    return rob.volatilityRegimes.length + rob.trendRegimes.length;
  }

  // ─── Research Limitations ───────────────────────────────

  get researchLimitations(): string[] {
    const limitations: string[] = [];
    const r = this.result();
    const rob = r.robustness;

    if (this.icDaysCount < 60)
      limitations.push(`Sample contains only ${this.icDaysCount} IC days — fewer than 3 months of trading data.`);
    if (r.effectiveN < 100)
      limitations.push(`Effective sample size (${Math.round(r.effectiveN)}) is below 100 after autocorrelation adjustment. Statistical tests have reduced power.`);
    if (this.coverageRatio < 0.50)
      limitations.push(`Coverage ratio is ${(this.coverageRatio * 100).toFixed(0)}% — significant gaps between requested and observed trading days.`);

    if (rob) {
      if (rob.volatilityRegimes.length < 3)
        limitations.push('Not all volatility regimes (Low/Normal/High) are represented in the sample.');
      if (rob.trendRegimes.length < 3)
        limitations.push('Not all trend regimes (Up/Sideways/Down) are represented in the sample.');
      if (rob.trainTest && rob.trainTest.testDays < 30)
        limitations.push(`Out-of-sample window is only ${rob.trainTest.testDays} days — too short for reliable OOS evaluation.`);
      if (!rob.trainTest)
        limitations.push('Insufficient data for train/test split analysis.');
      if (rob.monthlyBreakdown.length < 6)
        limitations.push(`Only ${rob.monthlyBreakdown.length} months of data — rolling stability metrics are unreliable.`);
    } else {
      limitations.push('No robustness analysis available — sample too small for stability assessment.');
    }

    if (!r.isStationary)
      limitations.push('Feature is non-stationary — statistical relationships may be spurious.');

    return limitations;
  }

  get hasLimitations(): boolean {
    return this.researchLimitations.length > 0;
  }

  // ─── Kill Criteria / Falsification ──────────────────────

  get falsificationCriteria(): FalsificationCriterion[] {
    const rob = this.result().robustness;

    return [
      {
        label: 'IC Collapse',
        description: 'Abandon if test-period Mean IC drops below 0.01 for 3+ consecutive months.',
        triggered: rob?.trainTest ? Math.abs(rob.trainTest.testMeanIC) < 0.01 : false,
      },
      {
        label: 'Regime Instability',
        description: 'Abandon if IC sign flips across majority of volatility or trend regimes.',
        triggered: this.isRegimeUnstable,
      },
      {
        label: 'Cost Erosion',
        description: 'Abandon if quantile spread (Q5-Q1) is less than 2x estimated round-trip costs.',
        triggered: this.quantileSpreadBps < 4,
      },
      {
        label: 'OOS Degradation',
        description: 'Abandon if OOS retention falls below 40% (likely overfit).',
        triggered: rob?.trainTest ? rob.trainTest.oosRetention < 0.40 : false,
      },
      {
        label: 'Structural Break',
        description: 'Re-evaluate if a significant structural break is detected in the IC series.',
        triggered: (rob?.structuralBreaks?.filter(b => b.significant).length ?? 0) > 0,
      },
    ];
  }

  get triggeredKillCriteria(): number {
    return this.falsificationCriteria.filter(c => c.triggered).length;
  }

  private get isRegimeUnstable(): boolean {
    const rob = this.result().robustness;
    if (!rob) return false;
    const allSigns = [
      ...rob.volatilityRegimes.map(r => Math.sign(r.meanIC)),
      ...rob.trendRegimes.map(r => Math.sign(r.meanIC)),
    ];
    if (allSigns.length < 2) return false;
    const positives = allSigns.filter(s => s > 0).length;
    const negatives = allSigns.filter(s => s < 0).length;
    return Math.min(positives, negatives) >= Math.max(positives, negatives) * 0.5;
  }

  // ─── Transaction Cost Sensitivity ──────────────────────

  get quantileSpread(): number {
    const bins = this.result().quantileBins;
    if (bins.length < 2) return 0;
    return Math.abs(bins[bins.length - 1].meanReturn - bins[0].meanReturn);
  }

  get quantileSpreadBps(): number {
    return this.quantileSpread * 10000;
  }

  get costScenarios(): CostScenario[] {
    const grossBps = this.quantileSpreadBps;
    return [1, 2, 5, 10].map(cost => ({
      costBps: cost,
      grossBps: Math.round(grossBps * 100) / 100,
      netBps: Math.round((grossBps - 2 * cost) * 100) / 100,
      viable: grossBps - 2 * cost > 0,
    }));
  }

  get netReturnAtSelectedCost(): number {
    return this.quantileSpreadBps - 2 * this.costBps();
  }

  get isCostViable(): boolean {
    return this.netReturnAtSelectedCost > 0;
  }

  get costErasureThreshold(): number {
    if (this.quantileSpreadBps <= 0) return 0;
    return Math.round(this.quantileSpreadBps / 2 * 100) / 100;
  }

  // ─── Statistical Assumptions ────────────────────────────

  get statisticalAssumptions(): { label: string; value: string }[] {
    const r = this.result();
    return [
      { label: 'Correlation Method', value: 'Spearman rank (nonparametric)' },
      { label: 'Prediction Horizon', value: '15 bars (15-minute forward return)' },
      { label: 'Quantile Bins', value: '5 (quintiles)' },
      { label: 'Execution Assumption', value: 'Next-bar open (no slippage modeled)' },
      { label: 'NW Lag Selection', value: 'Automatic Andrews (1991), max lag = floor(4 * (N/100)^(2/9))' },
      { label: 'IC Significance Threshold', value: 'p < 0.10 (NW-adjusted)' },
      { label: 'Stationarity Tests', value: 'ADF (p < 0.05) + KPSS (p > 0.05)' },
      { label: 'Monotonicity Threshold', value: '75% adjacent pairs ordered' },
      { label: 'Effective N', value: `${Math.round(r.effectiveN)} (of ${r.icDates.length} raw IC days)` },
      { label: 'Autocorrelation Adjustment', value: 'Truncated at rho_k < 0.05' },
    ];
  }

  // ─── Professional Tone (updated getters) ────────────────

  get validationSeverity(): 'success' | 'warn' | 'info' {
    if (this.isExploratoryOverride) return 'info';
    return this.result().passedValidation ? 'success' : 'warn';
  }

  get validationLabel(): string {
    if (this.isExploratoryOverride) return 'EXPLORATORY';
    return this.result().passedValidation ? 'PRELIMINARY EVIDENCE' : 'INSUFFICIENT EVIDENCE';
  }

  get validationVerdict(): string {
    const r = this.result();
    if (this.isExploratoryOverride) {
      return `${r.featureName} on ${r.ticker}: Insufficient data (${Math.round(r.effectiveN)} effective observations) ` +
        `for meaningful statistical validation. Results shown for directional reference only.`;
    }
    if (r.passedValidation) {
      const direction = r.meanIC < 0
        ? 'Negative IC is consistent with a mean-reversion (contrarian) hypothesis.'
        : 'Positive IC is consistent with a momentum hypothesis.';
      return `${r.featureName} shows preliminary evidence of predictive content for ${r.ticker} ` +
        `(Mean IC ${r.meanIC.toFixed(4)}, ${this.tStatLabel} ${this.displayedTStat.toFixed(2)}, ` +
        `p=${this.displayedPValue.toFixed(4)}). ${direction} ` +
        `These results require out-of-sample confirmation and are subject to the limitations noted below.`;
    }
    const issues: string[] = [];
    if (Math.abs(r.meanIC) < 0.03) issues.push('weak IC magnitude');
    if (Math.abs(this.displayedTStat) < 1.96) issues.push('insufficient statistical significance');
    if (!r.isStationary) issues.push('non-stationary feature');
    if (!r.isMonotonic) issues.push('non-monotonic quantile returns');
    return `${r.featureName} did not meet validation thresholds for ${r.ticker}: ${issues.join(', ')}. ` +
      `This does not rule out predictive content — consider a longer sample period or alternative feature transformations.`;
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
    const tier = this.confidenceTier;
    if (ic >= 0.1) return tier <= 2 ? 'Strong' : 'Strong (short-sample)';
    if (ic >= 0.05) return tier <= 2 ? 'Moderate' : 'Moderate (confirmation needed)';
    if (ic >= 0.03) return tier <= 2 ? 'Weak but detectable' : 'Marginal (low confidence)';
    return 'Below threshold';
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
      : 'Statistical significance of mean IC. |t| > 1.96 indicates significance at the 95% confidence level.';
  }

  get pValueTooltip(): string {
    return this.selectedTStatMethod() === 'nw'
      ? 'p-value from the Newey-West corrected t-stat. Accounts for autocorrelation in the IC series.'
      : 'Probability of observing this IC by chance. p < 0.05 indicates significance at 95% CI.';
  }

  // ─── Chart Rendering ───────────────────────────────────

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
            labels: { font: { size: 12 }, color: '#475569', padding: 16, usePointStyle: true },
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
            title: { display: true, text: 'IC (Spearman \u03C1)', font: { size: 13, weight: 'bold' }, color: '#475569' },
            ticks: { font: { size: 12 }, color: '#64748b' },
            grid: { color: '#f1f5f9' },
          },
          x: {
            title: { display: true, text: 'Date', font: { size: 13, weight: 'bold' }, color: '#475569' },
            ticks: { font: { size: 11 }, color: '#64748b', maxRotation: 45, maxTicksLimit: 12 },
            grid: { display: false },
          },
        },
      },
    });
  }

  private renderQuantileChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.quantileChart) this.quantileChart.destroy();

    const bins = res.quantileBins;
    const colors = bins.map(b => b.meanReturn >= 0 ? 'rgba(22, 163, 74, 0.75)' : 'rgba(220, 38, 38, 0.75)');
    const borderColors = bins.map(b => b.meanReturn >= 0 ? '#16a34a' : '#dc2626');

    this.quantileChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: bins.map(b => `Q${b.binNumber}`),
        datasets: [{
          label: 'Mean Forward Return',
          data: bins.map(b => b.meanReturn),
          backgroundColor: colors,
          borderColor: borderColors,
          borderWidth: 2,
          borderRadius: 4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: 'Quantile Mean Returns \u2014 E[R|Q]', font: { size: 15, weight: 'bold' }, color: '#1e293b', padding: { bottom: 16 } },
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1e293b', titleFont: { size: 13 }, bodyFont: { size: 12 }, padding: 10, cornerRadius: 6,
            callbacks: {
              label: (ctx) => {
                const bin = bins[ctx.dataIndex];
                return [`Mean Return: ${bin.meanReturn.toFixed(6)}`, `Range: [${bin.lowerBound.toFixed(4)}, ${bin.upperBound.toFixed(4)}]`, `Samples: ${bin.count}`];
              },
            },
          },
        },
        scales: {
          y: { title: { display: true, text: 'Mean Log Return', font: { size: 13, weight: 'bold' }, color: '#475569' }, ticks: { font: { size: 12 }, color: '#64748b' }, grid: { color: '#f1f5f9' } },
          x: { title: { display: true, text: 'Feature Quantile (Low \u2192 High)', font: { size: 13, weight: 'bold' }, color: '#475569' }, ticks: { font: { size: 13, weight: 'bold' }, color: '#334155' }, grid: { display: false } },
        },
      },
    });
  }

  private renderCumulativeIcChart(canvas: HTMLCanvasElement, res: ResearchResult): void {
    if (this.cumulativeIcChart) this.cumulativeIcChart.destroy();

    const cumIc: number[] = [];
    let sum = 0;
    for (const ic of res.icValues) { sum += ic; cumIc.push(sum); }

    this.cumulativeIcChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: res.icDates,
        datasets: [
          { label: 'Cumulative IC', data: cumIc, borderColor: '#10b981', backgroundColor: 'rgba(16, 185, 129, 0.08)', fill: true, tension: 0.3, pointRadius: 2, pointHoverRadius: 5, borderWidth: 2 },
          { label: 'Zero', data: res.icDates.map(() => 0), borderColor: '#cbd5e1', borderDash: [3, 3], pointRadius: 0, borderWidth: 1 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          title: { display: true, text: 'Cumulative Information Coefficient (\u03A3 IC)', font: { size: 15, weight: 'bold' }, color: '#1e293b', padding: { bottom: 16 } },
          legend: { position: 'bottom', labels: { font: { size: 12 }, color: '#475569', padding: 16, usePointStyle: true, filter: (item) => item.text !== 'Zero' } },
          tooltip: {
            backgroundColor: '#1e293b', titleFont: { size: 13 }, bodyFont: { size: 12 }, padding: 10, cornerRadius: 6,
            callbacks: { label: (ctx) => { if (ctx.dataset.label === 'Zero') return ''; return `Cumulative IC: ${Number(ctx.raw).toFixed(4)}`; } },
          },
        },
        scales: {
          y: { title: { display: true, text: 'Cumulative IC', font: { size: 13, weight: 'bold' }, color: '#475569' }, ticks: { font: { size: 12 }, color: '#64748b' }, grid: { color: '#f1f5f9' } },
          x: { title: { display: true, text: 'Date', font: { size: 13, weight: 'bold' }, color: '#475569' }, ticks: { font: { size: 11 }, color: '#64748b', maxRotation: 45, maxTicksLimit: 12 }, grid: { display: false } },
        },
      },
    });
  }
}
