import {
  Component,
  signal,
  computed,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  ElementRef,
  viewChild,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { InputNumber } from 'primeng/inputnumber';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ProgressSpinner } from 'primeng/progressspinner';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { TooltipModule } from 'primeng/tooltip';
import { Checkbox } from 'primeng/checkbox';
import { MultiSelect } from 'primeng/multiselect';
import { Chart, registerables } from 'chart.js';
import { InfoIconComponent } from '../../../shared/info-icon/info-icon.component';
import { MethodologyDrawerService } from '../../../shared/methodology-drawer/methodology-drawer.service';
import {
  IndicatorVerdictHeroComponent,
  type VerdictAnalysis,
  type VerdictCta,
} from '../../../shared/indicator-verdict-hero';

Chart.register(...registerables);

// ─── Interfaces ────────────────────────────────────────────

type StrengthLabel = 'Noise' | 'Weak' | 'Moderate' | 'Strong';
type StabilityLabel = 'Low' | 'Moderate' | 'High';
type DirectionLabel = 'Mean-Reversion' | 'Momentum' | 'None';
type TradeabilityLabel = 'Likely tradeable' | 'Marginal' | 'Unlikely' | 'Unknown';

interface Verdict {
  direction: DirectionLabel;
  strength: StrengthLabel;
  stability: StabilityLabel;
  tradeability: TradeabilityLabel;
  horizon: number | null;
  tradeability_caveat: string | null;
}

interface DecayCurvePoint {
  horizon: number;
  ic: number;
  p_value: number;
  ic_stderr: number;
}

interface RegimeICPoint {
  horizon: number;
  mean_ic: number;
  t_stat: number;
  p_value: number;
  effective_n: number;
  hit_rate: number;
  bars_in_regime: number;
}

interface RegimeResults {
  high_vol: RegimeICPoint[] | null;
  low_vol: RegimeICPoint[] | null;
  vol_window: number;
}

interface HorizonICResult {
  horizon: number;
  // In-sample
  is_mean_ic: number;
  is_t_stat: number;
  is_p_value: number;
  is_nw_t_stat: number | null;
  is_nw_p_value: number | null;
  is_effective_n: number;
  // Out-of-sample
  oos_mean_ic: number | null;
  oos_t_stat: number | null;
  oos_p_value: number | null;
  oos_effective_n: number | null;
  oos_retention: number | null;
  // Multiple testing
  bonferroni_p: number;
  fdr_p: number;
  // Random baseline
  random_baseline_mean: number;
  random_baseline_std: number;
  ic_vs_random_zscore: number;
  // Interpretations (legacy)
  is_interpretation: string;
  oos_interpretation: string | null;
  // Stability
  is_hit_rate: number;
  is_daily_ic_std: number;
  // Verdict labels
  strength_label: StrengthLabel;
  stability_label: StabilityLabel;
  direction_label: DirectionLabel;
  // OOS delta as +% / -%, null if OOS missing
  retention_delta_pct: number | null;
  // Slope decision flags (populated on slope variant rows only)
  slope_adds_value: boolean | null;
  slope_recommended: boolean | null;
  // IR proxy
  annualized_ir: number;
  sharpe_estimate: number;
  breadth_per_year: number;
  // Random baseline distribution (populated only for best horizon)
  random_baseline_distribution: number[];
}

interface IndicatorReliabilityResponse {
  success: boolean;
  ticker: string;
  indicator_name: string;
  indicator_params: Record<string, number>;
  display_name: string;
  category: string | null;
  start_date: string;
  end_date: string;
  bar_count: number;
  // Train/test split
  train_start: string | null;
  train_end: string | null;
  test_start: string | null;
  test_end: string | null;
  train_bars: number | null;
  test_bars: number | null;
  train_ratio: number;
  // Results
  results: HorizonICResult[];
  slope_results: HorizonICResult[] | null;
  daily_ic_values: number[];
  daily_ic_dates: string[];
  best_horizon: number | null;
  // Multiple testing
  any_significant_after_bonferroni: boolean;
  any_significant_after_fdr: boolean;
  num_horizons_tested: number;
  random_simulations: number;
  // Top-line verdict
  verdict: Verdict | null;
  // P2 diagnostics
  decay_curve: DecayCurvePoint[];
  regime_results: RegimeResults | null;
  // P3 economic layer & honesty
  next_steps: string[];
  info_footnotes: string[];
  // Warnings
  warnings: string[];
  error: string | null;
}

interface IndicatorInfo {
  name: string;
  category: string;
  description: string;
  params: ParamConfig[];
}

interface ParamConfig {
  name: string;
  type: string;
  default: number;
  min?: number;
  max?: number;
  description: string;
}

interface IndicatorOption {
  label: string;
  value: string;
  category: string;
  description: string;
}

interface HorizonOption {
  label: string;
  value: number;
}

@Component({
  selector: 'app-indicator-reliability',
  templateUrl: './indicator-reliability.component.html',
  styleUrls: ['./indicator-reliability.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    FormsModule,
    Select,
    InputText,
    InputNumber,
    ButtonModule,
    MessageModule,
    ProgressSpinner,
    TableModule,
    TagModule,
    TooltipModule,
    Checkbox,
    MultiSelect,
    InfoIconComponent,
    IndicatorVerdictHeroComponent,
  ],
})
export class IndicatorReliabilityComponent {
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);
  private pythonUrl = environment.pythonServiceUrl;
  protected methodologyDrawer = inject(MethodologyDrawerService);

  // Expose Math for template use
  protected readonly Math = Math;

  icChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('icChart');
  decayChartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('decayChart');
  baselineHistCanvas = viewChild<ElementRef<HTMLCanvasElement>>('baselineHist');
  private icChart: Chart | null = null;
  private decayChart: Chart | null = null;
  private baselineHistChart: Chart | null = null;

  // Rolling window for the IC chart overlay (bars).
  private readonly ROLLING_IC_WINDOW = 20;

  // Form inputs
  ticker = signal('AAPL');
  indicatorName = signal('rsi');
  fromDate = signal('2024-01-01');
  toDate = signal('2024-06-30');
  includeSlope = signal(false);

  // Dynamic params (populated when indicator is selected)
  paramConfigs = signal<ParamConfig[]>([]);
  paramValues = signal<Record<string, number>>({});

  // Horizons multi-select
  allHorizons: HorizonOption[] = [
    { label: '1-bar', value: 1 },
    { label: '5-bar', value: 5 },
    { label: '10-bar', value: 10 },
    { label: '15-bar', value: 15 },
    { label: '30-bar', value: 30 },
  ];
  selectedHorizons = signal<number[]>([1, 5, 10, 15, 30]);

  // State
  loading = signal(false);
  loadingIndicators = signal(false);
  result = signal<IndicatorReliabilityResponse | null>(null);
  error = signal<string | null>(null);
  formCollapsed = signal(false);

  /**
   * Slim projection of the current result into the shape the shared
   * ``<app-indicator-verdict-hero>`` consumes. Rebuilt on every result
   * change so the hero always reflects the freshest analysis without
   * the hero having to know about the full response type.
   */
  readonly verdictAnalysis = computed<VerdictAnalysis | null>(() => {
    const res = this.result();
    if (!res || !res.success) return null;
    const best = res.results.find((r) => r.horizon === res.best_horizon)
      ?? res.results[0];
    if (!best) return null;

    const oosHolds =
      best.oos_retention !== null ? best.oos_retention >= 0.6 : true;
    const economicallyMeaningful = Math.abs(best.oos_mean_ic ?? best.is_mean_ic) > 0.1;

    const indicatorDisplay = res.display_name
      || `${res.indicator_name.toUpperCase()}`;

    const directionLabel =
      best.direction_label === 'Mean-Reversion' ? 'mean-reversion'
      : best.direction_label === 'Momentum' ? 'trend-following'
      : 'directional';

    const highVolBoostPct = (() => {
      const hv = res.regime_results?.high_vol?.find((c) => c.horizon === best.horizon);
      const lv = res.regime_results?.low_vol?.find((c) => c.horizon === best.horizon);
      if (!hv || !lv || lv.mean_ic === 0) return null;
      return Math.round(((Math.abs(hv.mean_ic) - Math.abs(lv.mean_ic)) / Math.abs(lv.mean_ic)) * 100);
    })();

    return {
      indicatorDisplay,
      ticker: res.ticker,
      bestHorizonLabel: `${best.horizon}-bar`,
      oosIc: best.oos_mean_ic ?? best.is_mean_ic,
      isIc: best.is_mean_ic,
      oosVsIsPct: best.retention_delta_pct,
      sharpe: best.sharpe_estimate,
      fdrSignificant: res.any_significant_after_fdr,
      bonferroniSignificant: res.any_significant_after_bonferroni,
      oosHolds,
      zScore: best.ic_vs_random_zscore,
      economicallyMeaningful,
      highVolHitRate: res.regime_results?.high_vol?.find((c) => c.horizon === best.horizon)?.hit_rate ?? null,
      lowVolHitRate: res.regime_results?.low_vol?.find((c) => c.horizon === best.horizon)?.hit_rate ?? null,
      highVolBoostPct,
      singleAsset: true,
      directionLabel,
      randomShuffles: res.random_simulations,
    };
  });

  onVerdictCta(kind: VerdictCta): void {
    // These are advisory emits today — each CTA is resolved later when
    // the corresponding flow exists. For now we write intent to the
    // console so the wiring is observable in dev, and the verdict panel
    // stays useful as a read-only summary.
    // eslint-disable-next-line no-console
    console.info('[indicator-reliability] verdict CTA:', kind);
  }

  // Indicator options (loaded from API)
  indicatorOptions = signal<IndicatorOption[]>([]);
  groupedIndicators = signal<Record<string, IndicatorOption[]>>({});

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.indicatorName().trim().length > 0 &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
      this.selectedHorizons().length > 0 &&
      !this.loading()
    );
  });

  get selectedIndicatorLabel(): string {
    const found = this.indicatorOptions().find(i => i.value === this.indicatorName());
    return found ? found.label : this.indicatorName().toUpperCase();
  }

  constructor() {
    // Load indicators on init
    this.loadIndicators();

    // Load params when indicator changes
    effect(() => {
      const name = this.indicatorName();
      if (name) {
        this.loadIndicatorParams(name);
      }
    });

    // Render chart when result changes
    effect(() => {
      const res = this.result();
      const canvas = this.icChartCanvas();
      if (res && canvas && res.daily_ic_values.length > 0) {
        this.renderIcChart(canvas.nativeElement, res);
      }
    });

    // Render decay curve chart when result changes
    effect(() => {
      const res = this.result();
      const canvas = this.decayChartCanvas();
      if (res && canvas && res.decay_curve && res.decay_curve.length > 0) {
        this.renderDecayChart(canvas.nativeElement, res.decay_curve);
      }
    });

    // Render baseline histogram when result changes
    effect(() => {
      const res = this.result();
      const canvas = this.baselineHistCanvas();
      if (!res || !canvas) return;
      const best = this.getBestRandomResult();
      if (best && best.random_baseline_distribution.length > 0) {
        this.renderBaselineHistogram(
          canvas.nativeElement,
          best.random_baseline_distribution,
          best.is_mean_ic,
        );
      }
    });
  }

  private loadIndicators(): void {
    this.loadingIndicators.set(true);
    this.http
      .get<Record<string, IndicatorInfo[]>>(`${this.pythonUrl}/api/research/indicators`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          console.error('Failed to load indicators:', err);
          return of({});
        }),
        finalize(() => this.loadingIndicators.set(false)),
      )
      .subscribe(categories => {
        const options: IndicatorOption[] = [];
        const grouped: Record<string, IndicatorOption[]> = {};

        for (const [category, indicators] of Object.entries(categories)) {
          grouped[category] = [];
          for (const ind of indicators) {
            const opt: IndicatorOption = {
              label: ind.name.toUpperCase(),
              value: ind.name,
              category,
              description: ind.description,
            };
            options.push(opt);
            grouped[category].push(opt);
          }
        }

        this.indicatorOptions.set(options);
        this.groupedIndicators.set(grouped);
      });
  }

  private loadIndicatorParams(name: string): void {
    this.http
      .get<ParamConfig[]>(`${this.pythonUrl}/api/research/indicator-params/${name}`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(() => of([])),
      )
      .subscribe(params => {
        this.paramConfigs.set(params);
        // Set default values
        const defaults: Record<string, number> = {};
        for (const p of params) {
          defaults[p.name] = p.default;
        }
        this.paramValues.set(defaults);
      });
  }

  updateParam(name: string, value: number): void {
    this.paramValues.update(current => ({ ...current, [name]: value }));
  }

  runAnalysis(): void {
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);

    const payload = {
      ticker: this.ticker().toUpperCase(),
      indicator_name: this.indicatorName(),
      indicator_params: this.paramValues(),
      start_date: this.fromDate(),
      end_date: this.toDate(),
      horizons: this.selectedHorizons(),
      include_slope: this.includeSlope(),
      timespan: 'minute',
      multiplier: 1,
    };

    this.http
      .post<IndicatorReliabilityResponse>(
        `${this.pythonUrl}/api/research/indicator-reliability`,
        payload,
      )
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          const msg = err?.error?.detail ?? err?.message ?? 'An unexpected error occurred';
          this.error.set(msg);
          return of(null);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(res => {
        if (res) {
          this.result.set(res);
          if (!res.success && res.error) {
            this.error.set(res.error);
          } else if (res.success) {
            this.formCollapsed.set(true);
          }
        }
      });
  }

  toggleForm(): void {
    this.formCollapsed.update(v => !v);
  }

  newRun(): void {
    this.formCollapsed.set(false);
    this.result.set(null);
    this.error.set(null);
  }

  // ─── Helpers for Display ─────────────────────────────────

  getIsSeverity(row: HorizonICResult): 'success' | 'warn' | 'danger' | 'info' {
    if (row.fdr_p >= 0.10) return 'danger';
    if (row.fdr_p < 0.05 && Math.abs(row.is_mean_ic) >= 0.03) return 'success';
    if (row.fdr_p < 0.10) return 'warn';
    return 'info';
  }

  getOosSeverity(row: HorizonICResult): 'success' | 'warn' | 'danger' | 'info' {
    if (row.oos_p_value === null) return 'info';
    if (row.oos_p_value >= 0.10) return 'danger';
    if (row.oos_retention !== null && row.oos_retention >= 0.6 && row.oos_p_value < 0.05)
      return 'success';
    if (row.oos_p_value < 0.10) return 'warn';
    return 'info';
  }

  getRetentionSeverity(retention: number | null): 'success' | 'warn' | 'danger' | 'info' {
    if (retention === null) return 'info';
    if (retention >= 0.6) return 'success';
    if (retention >= 0.4) return 'warn';
    return 'danger';
  }

  formatIc(ic: number | null): string {
    if (ic === null) return '-';
    return ic.toFixed(4);
  }

  formatPValue(p: number | null): string {
    if (p === null) return '-';
    if (p < 0.001) return '<0.001';
    return p.toFixed(3);
  }

  formatRetention(r: number | null): string {
    if (r === null) return '-';
    return `${(r * 100).toFixed(0)}%`;
  }

  formatRetentionDelta(delta: number | null): string {
    if (delta === null) return '-';
    const sign = delta >= 0 ? '+' : '';
    return `${sign}${delta.toFixed(0)}%`;
  }

  getRetentionDeltaSeverity(delta: number | null): 'success' | 'warn' | 'danger' | 'info' {
    if (delta === null) return 'info';
    if (delta >= -20) return 'success'; // OOS stronger, flat, or slightly weaker
    if (delta >= -60) return 'warn';
    return 'danger';
  }

  formatZScore(z: number): string {
    return `${z >= 0 ? '+' : ''}${z.toFixed(1)}σ`;
  }

  formatSharpe(s: number): string {
    return `${s >= 0 ? '+' : ''}${s.toFixed(2)}`;
  }

  getSharpeSeverity(s: number): 'success' | 'warn' | 'danger' | 'info' {
    const abs = Math.abs(s);
    if (abs >= 1.0) return 'success';
    if (abs >= 0.5) return 'warn';
    if (abs >= 0.2) return 'info';
    return 'danger';
  }

  getStrengthSeverity(label: StrengthLabel): 'success' | 'warn' | 'danger' | 'info' {
    if (label === 'Strong') return 'success';
    if (label === 'Moderate') return 'info';
    if (label === 'Weak') return 'warn';
    return 'danger'; // Noise
  }

  getStabilitySeverity(label: StabilityLabel): 'success' | 'warn' | 'danger' {
    if (label === 'High') return 'success';
    if (label === 'Moderate') return 'warn';
    return 'danger';
  }

  getDirectionSeverity(label: DirectionLabel): 'info' | 'danger' {
    return label === 'None' ? 'danger' : 'info';
  }

  // ─── Mission-control verdict helpers (T1) ─────────────────

  /** Ordered list of horizon rows in the IS table. */
  getHorizonRows(): HorizonICResult[] {
    return this.result()?.results ?? [];
  }

  /** Best-horizon row (or null). Drives the verdict hero + decision cells. */
  getBestRow(): HorizonICResult | null {
    const r = this.result();
    if (!r?.best_horizon) return null;
    return r.results.find(h => h.horizon === r.best_horizon) ?? null;
  }

  /**
   * Confidence score (0–100) — mirrors the design bundle's gauge formula.
   * 20 points each for: FDR significant, Bonferroni significant, OOS holds
   * (retention >= -30% OR positive), |z vs random| > 3, |best IC| > 0.10
   * (partial credit: 10 points if |IC| > 0, 0 otherwise).
   */
  getConfidenceScore(): number {
    const res = this.result();
    const best = this.getBestRow();
    if (!res || !best) return 0;

    const fdrOk = res.any_significant_after_fdr ? 20 : 0;
    const bonfOk = res.any_significant_after_bonferroni ? 20 : 0;

    // OOS holds: delta >= -30% OR positive. Treat missing OOS as 0.
    const delta = best.retention_delta_pct;
    const oosOk = delta !== null && (delta >= -30 || delta > 0) ? 20 : 0;

    const z = Math.abs(best.ic_vs_random_zscore);
    const randomOk = z > 3 ? 20 : 0;

    const absIc = Math.abs(best.is_mean_ic);
    const icOk = absIc > 0.10 ? 20 : absIc > 0 ? 10 : 0;

    return fdrOk + bonfOk + oosOk + randomOk + icOk;
  }

  /** Verdict bucket from the score — drives gauge color + headline verb. */
  getConfidenceBucket(): 'TRADE' | 'INVESTIGATE' | 'REJECT' {
    const s = this.getConfidenceScore();
    if (s >= 85) return 'TRADE';
    if (s >= 60) return 'INVESTIGATE';
    return 'REJECT';
  }

  /** CSS-friendly color for the confidence bucket. */
  getConfidenceColor(): string {
    const b = this.getConfidenceBucket();
    if (b === 'TRADE') return 'var(--bull)';
    if (b === 'INVESTIGATE') return 'var(--warn)';
    return 'var(--bear)';
  }

  /** Verb shown in the hero headline. */
  getVerdictVerb(): string {
    const b = this.getConfidenceBucket();
    if (b === 'TRADE') return 'Ready to trade';
    if (b === 'INVESTIGATE') return 'Investigate further';
    return 'Do not trade';
  }

  /**
   * Arc dash-length for the gauge. The gauge spans 3/4 of a circle (C * 0.75);
   * we multiply by the score fraction to fill that arc proportionally.
   */
  getGaugeDash(): { filled: number; full: number; circumference: number } {
    const R = 90;
    const C = 2 * Math.PI * R;
    const arc = C * 0.75;
    const filled = (this.getConfidenceScore() / 100) * arc;
    return { filled, full: arc, circumference: C };
  }

  /** Reason chips for the hero — pass/fail driven, pulled from the result. */
  getReasonPills(): { label: string; kind: 'good' | 'warn' | 'neutral' }[] {
    const res = this.result();
    const best = this.getBestRow();
    if (!res || !best) return [];

    const pills: { label: string; kind: 'good' | 'warn' | 'neutral' }[] = [];
    pills.push({ label: res.any_significant_after_fdr ? 'FDR ✓' : 'FDR ✗', kind: res.any_significant_after_fdr ? 'good' : 'warn' });
    pills.push({ label: res.any_significant_after_bonferroni ? 'Bonferroni ✓' : 'Bonferroni ✗', kind: res.any_significant_after_bonferroni ? 'good' : 'warn' });

    const delta = best.retention_delta_pct;
    if (delta !== null) {
      const sign = delta >= 0 ? '+' : '';
      const ok = delta >= -30;
      pills.push({ label: `OOS holds (${sign}${delta.toFixed(0)}%)`, kind: ok ? 'good' : 'warn' });
    }

    const absIc = Math.abs(best.oos_mean_ic ?? best.is_mean_ic);
    pills.push({ label: `|IC| ${absIc.toFixed(3)} ${absIc > 0.10 ? '> 0.10' : '≤ 0.10'}`, kind: absIc > 0.10 ? 'good' : 'warn' });

    // Regime insight if available
    const rc = this.getRegimeComparison();
    if (rc) {
      pills.push({ label: `Stronger in ${rc.strongerLabel}`, kind: 'good' });
    }

    // Always-on honesty chip
    pills.push({ label: 'Single asset only', kind: 'neutral' });

    return pills;
  }

  /** WHEN cell content — "Hold {best}-bar" + decay detail. */
  getWhenCell(): { answer: string; detail: string } | null {
    const res = this.result();
    const best = this.getBestRow();
    if (!res || !best) return null;

    const answer = `Hold ${best.horizon}-bar`;
    const curve = res.decay_curve;
    let detail: string;
    if (curve && curve.length > 0) {
      const peak = curve.reduce((acc, p) => (Math.abs(p.ic) > Math.abs(acc.ic) ? p : acc), curve[0]);
      detail = `IC peaks at ${peak.horizon}-bar (${peak.ic.toFixed(3)}), ${Math.abs(peak.ic) > 0.10 ? 'decays slowly' : 'fades quickly'} after.`;
    } else {
      detail = `Best horizon by OOS significance.`;
    }
    return { answer, detail };
  }

  /** Compare regime ICs at the best horizon; returns the stronger regime. */
  getRegimeComparison():
    | { strongerLabel: 'high-vol regimes' | 'low-vol regimes'; deltaPct: number; highIc: number; lowIc: number; highHit: number; lowHit: number }
    | null {
    const res = this.result();
    const best = this.getBestRow();
    if (!res?.regime_results || !best) return null;
    const high = res.regime_results.high_vol?.find(p => p.horizon === best.horizon);
    const low = res.regime_results.low_vol?.find(p => p.horizon === best.horizon);
    if (!high || !low) return null;
    const hAbs = Math.abs(high.mean_ic);
    const lAbs = Math.abs(low.mean_ic);
    if (hAbs === 0 && lAbs === 0) return null;
    const stronger = hAbs >= lAbs ? 'high-vol regimes' : 'low-vol regimes';
    const base = stronger === 'high-vol regimes' ? lAbs : hAbs;
    const delta = base > 1e-10 ? (Math.abs(hAbs - lAbs) / base) * 100 : 0;
    return {
      strongerLabel: stronger,
      deltaPct: delta,
      highIc: high.mean_ic,
      lowIc: low.mean_ic,
      highHit: high.hit_rate,
      lowHit: low.hit_rate,
    };
  }

  /** WHERE cell — regime answer + detail. */
  getWhereCell(): { answer: string; detail: string } | null {
    const rc = this.getRegimeComparison();
    if (!rc) return null;
    const answer = rc.strongerLabel === 'high-vol regimes' ? 'High-vol regimes' : 'Low-vol regimes';
    const detail =
      `+${rc.deltaPct.toFixed(0)}% stronger when vol is ${rc.strongerLabel.includes('high') ? 'above' : 'below'} median ` +
      `(IC ${rc.strongerLabel.includes('high') ? rc.highIc.toFixed(3) : rc.lowIc.toFixed(3)} vs ` +
      `${rc.strongerLabel.includes('high') ? rc.lowIc.toFixed(3) : rc.highIc.toFixed(3)}). ` +
      `Hit-rate ${((rc.strongerLabel.includes('high') ? rc.highHit : rc.lowHit) * 100).toFixed(0)}% vs ` +
      `${((rc.strongerLabel.includes('high') ? rc.lowHit : rc.highHit) * 100).toFixed(0)}%.`;
    return { answer, detail };
  }

  /** HOW cell — threshold rule + sharpe proxy. */
  getHowCell(): { answer: string; detail: string } | null {
    const best = this.getBestRow();
    if (!best) return null;
    const dir = best.direction_label;
    let answer: string;
    if (dir === 'Mean-Reversion') {
      answer = 'Fade extremes';
    } else if (dir === 'Momentum') {
      answer = 'Follow the move';
    } else {
      answer = 'No clear edge';
    }
    const detail =
      `Sharpe proxy ${this.formatSharpe(best.sharpe_estimate)}. ` +
      `${dir === 'Mean-Reversion' ? 'Short when indicator is high, long when low.' : dir === 'Momentum' ? 'Long when indicator is high, short when low.' : 'Signal too weak to trade.'} ` +
      `Test with costs before sizing.`;
    return { answer, detail };
  }

  /** 5-test decision checklist — each with a pass/fail + one-line detail. */
  getChecklist(): { pass: boolean; label: string; detail: string }[] {
    const res = this.result();
    const best = this.getBestRow();
    if (!res || !best) return [];

    const items: { pass: boolean; label: string; detail: string }[] = [];

    items.push({
      pass: res.any_significant_after_fdr,
      label: 'FDR significance',
      detail: `p < 0.05 at ${this.countFdrPasses()}/${res.num_horizons_tested} horizons`,
    });

    items.push({
      pass: res.any_significant_after_bonferroni,
      label: 'Bonferroni (conservative)',
      detail: res.any_significant_after_bonferroni ? 'Passes the strictest correction' : 'Fails strictest correction',
    });

    const delta = best.retention_delta_pct;
    const oosPass = delta !== null && (delta >= -40 || delta > 0);
    items.push({
      pass: oosPass,
      label: 'Out-of-sample holds',
      detail: delta === null
        ? 'No OOS data yet'
        : `OOS ${best.oos_mean_ic?.toFixed(3) ?? '-'} vs IS ${best.is_mean_ic.toFixed(3)} (${delta >= 0 ? '+' : ''}${delta.toFixed(0)}%)`,
    });

    const z = Math.abs(best.ic_vs_random_zscore);
    items.push({
      pass: z > 3,
      label: 'Beats random',
      detail: `${z.toFixed(1)}σ above noise floor`,
    });

    const absIc = Math.abs(best.oos_mean_ic ?? best.is_mean_ic);
    items.push({
      pass: absIc > 0.10,
      label: 'Economically meaningful',
      detail: `|IC| ${absIc.toFixed(3)} ${absIc > 0.10 ? '>' : '≤'} 0.10 threshold`,
    });

    return items;
  }

  /** Count of horizons that cleared FDR — used in checklist detail. */
  countFdrPasses(): number {
    return this.getHorizonRows().filter(r => r.fdr_p < 0.05).length;
  }

  /** Horizon compact cards — used in the content grid. Preserves input order. */
  getHorizonCards(): HorizonICResult[] {
    return this.getHorizonRows();
  }

  /** Prompt + re-run with a new ticker (only wired CTA). */
  runOnAnotherTicker(): void {
    const current = this.ticker();
    const next = window.prompt('Run on another ticker (e.g. MSFT, GOOGL, NVDA):', current);
    if (!next) return;
    const cleaned = next.trim().toUpperCase();
    if (!cleaned || cleaned === current) return;
    this.ticker.set(cleaned);
    this.runAnalysis();
  }

  /**
   * Noise-floor bar positions. The band shown is ±1σ around the random IC mean,
   * mapped into a [0, 100] x-range for rendering. Your IC is placed relative
   * to that range and clamped so extreme values still render on-bar.
   */
  getNoiseFloorBar(): { bandLeftPct: number; bandWidthPct: number; icPct: number; center: number } | null {
    const res = this.result();
    const best = this.getBestRow();
    if (!res || !best) return null;
    const mean = best.random_baseline_mean;
    const std = best.random_baseline_std;
    if (std < 1e-10) return null;

    // Build a [-4σ, +4σ] range around the random mean, then plot best's IC.
    const range = 4 * std;
    const lo = mean - range;
    const hi = mean + range;
    const span = hi - lo;
    const bandLeftPct = ((mean - std - lo) / span) * 100;
    const bandWidthPct = ((2 * std) / span) * 100;
    const icRaw = ((best.is_mean_ic - lo) / span) * 100;
    const icPct = Math.max(2, Math.min(98, icRaw));
    return { bandLeftPct, bandWidthPct, icPct, center: 50 };
  }

  getTradeabilitySeverity(
    label: TradeabilityLabel,
  ): 'success' | 'warn' | 'danger' | 'info' {
    if (label === 'Likely tradeable') return 'success';
    if (label === 'Marginal') return 'warn';
    if (label === 'Unlikely') return 'danger';
    return 'info';
  }

  getSlopeDecisionSeverity(flag: boolean | null): 'success' | 'danger' | 'info' {
    if (flag === null) return 'info';
    return flag ? 'success' : 'danger';
  }

  formatSlopeDecision(flag: boolean | null): string {
    if (flag === null) return '-';
    return flag ? 'YES' : 'NO';
  }

  isBestHorizon(horizon: number): boolean {
    return this.result()?.best_horizon === horizon;
  }

  getBestRandomZScore(): number {
    const res = this.result();
    if (!res) return 0;
    return Math.max(...res.results.map(r => Math.abs(r.ic_vs_random_zscore)));
  }

  getBestRandomResult(): HorizonICResult | null {
    const res = this.result();
    if (!res || res.results.length === 0) return null;
    return res.results.reduce((best, curr) =>
      Math.abs(curr.ic_vs_random_zscore) > Math.abs(best.ic_vs_random_zscore) ? curr : best,
    );
  }

  // ─── Chart Rendering ─────────────────────────────────────

  /** Rolling mean with NaNs for the warmup period (first window-1 points). */
  private rollingMean(values: number[], window: number): (number | null)[] {
    const out: (number | null)[] = new Array(values.length).fill(null);
    if (values.length < window) return out;
    let sum = 0;
    for (let i = 0; i < window; i++) sum += values[i];
    out[window - 1] = sum / window;
    for (let i = window; i < values.length; i++) {
      sum += values[i] - values[i - window];
      out[i] = sum / window;
    }
    return out;
  }

  private renderIcChart(canvas: HTMLCanvasElement, res: IndicatorReliabilityResponse): void {
    if (this.icChart) this.icChart.destroy();

    const meanIc =
      res.daily_ic_values.reduce((a, b) => a + b, 0) / res.daily_ic_values.length || 0;
    const meanLine = res.daily_ic_dates.map(() => meanIc);
    const zeroLine = res.daily_ic_dates.map(() => 0);
    const rolling = this.rollingMean(res.daily_ic_values, this.ROLLING_IC_WINDOW);

    this.icChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: res.daily_ic_dates,
        datasets: [
          {
            label: 'Daily IC (In-Sample)',
            data: res.daily_ic_values,
            borderColor: '#93c5fd',
            backgroundColor: 'rgba(147, 197, 253, 0.15)',
            fill: true,
            tension: 0.3,
            pointRadius: 1,
            pointHoverRadius: 4,
            borderWidth: 1,
          },
          {
            label: `${this.ROLLING_IC_WINDOW}-day Rolling Mean`,
            data: rolling,
            borderColor: '#1d4ed8',
            backgroundColor: 'transparent',
            fill: false,
            tension: 0.25,
            pointRadius: 0,
            borderWidth: 2.5,
            spanGaps: false,
          },
          {
            label: `Mean IC (${meanIc.toFixed(4)})`,
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
            text: `Daily IC Time Series — In-Sample Period (${res.train_start} to ${res.train_end})`,
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
              filter: item => item.text !== 'Zero',
            },
          },
          tooltip: {
            backgroundColor: '#1e293b',
            titleFont: { size: 13 },
            bodyFont: { size: 12 },
            padding: 10,
            cornerRadius: 6,
            callbacks: {
              label: ctx => {
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
              text: 'IC (Spearman ρ)',
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

  private renderDecayChart(canvas: HTMLCanvasElement, curve: DecayCurvePoint[]): void {
    if (this.decayChart) this.decayChart.destroy();

    const labels = curve.map(p => `${p.horizon}`);
    const ics = curve.map(p => p.ic);
    const upper = curve.map(p => p.ic + 1.96 * p.ic_stderr);
    const lower = curve.map(p => p.ic - 1.96 * p.ic_stderr);
    const zero = curve.map(() => 0);

    // Peak = horizon with max |IC|
    let peakIdx = 0;
    for (let i = 1; i < curve.length; i++) {
      if (Math.abs(curve[i].ic) > Math.abs(curve[peakIdx].ic)) peakIdx = i;
    }
    const peakRadius = curve.map((_, i) => (i === peakIdx ? 6 : 0));
    const peak = curve[peakIdx];

    this.decayChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Upper 95% CI',
            data: upper,
            borderColor: 'rgba(59, 130, 246, 0.25)',
            backgroundColor: 'rgba(59, 130, 246, 0.12)',
            borderWidth: 1,
            pointRadius: 0,
            fill: '+1', // fill to next dataset (lower bound)
            tension: 0.2,
          },
          {
            label: 'Lower 95% CI',
            data: lower,
            borderColor: 'rgba(59, 130, 246, 0.25)',
            backgroundColor: 'rgba(59, 130, 246, 0.12)',
            borderWidth: 1,
            pointRadius: 0,
            fill: false,
            tension: 0.2,
          },
          {
            label: 'IC',
            data: ics,
            borderColor: '#1d4ed8',
            backgroundColor: 'transparent',
            borderWidth: 2.5,
            tension: 0.2,
            pointRadius: peakRadius,
            pointBackgroundColor: '#f59e0b',
            pointBorderColor: '#b45309',
            pointBorderWidth: 2,
            fill: false,
          },
          {
            label: 'Zero',
            data: zero,
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
            text: `IC Decay Curve — Peak at ${peak.horizon}-bar (IC = ${peak.ic.toFixed(4)})`,
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
              filter: item =>
                item.text !== 'Zero' &&
                item.text !== 'Upper 95% CI' &&
                item.text !== 'Lower 95% CI',
            },
          },
          tooltip: {
            backgroundColor: '#1e293b',
            callbacks: {
              label: ctx => {
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
              text: 'IC (Spearman ρ)',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: { font: { size: 12 }, color: '#64748b' },
            grid: { color: '#f1f5f9' },
          },
          x: {
            title: {
              display: true,
              text: 'Forward horizon (bars)',
              font: { size: 13, weight: 'bold' },
              color: '#475569',
            },
            ticks: { font: { size: 11 }, color: '#64748b' },
            grid: { display: false },
          },
        },
      },
    });
  }

  private renderBaselineHistogram(
    canvas: HTMLCanvasElement,
    distribution: number[],
    actualIc: number,
  ): void {
    if (this.baselineHistChart) this.baselineHistChart.destroy();

    const nBins = 15;
    const values = [...distribution, actualIc];
    const min = Math.min(...values);
    const max = Math.max(...values);
    const pad = (max - min) * 0.05 || 0.001;
    const lo = min - pad;
    const hi = max + pad;
    const width = (hi - lo) / nBins;

    const bins = new Array(nBins).fill(0);
    const binCenters: number[] = [];
    for (let i = 0; i < nBins; i++) binCenters.push(lo + (i + 0.5) * width);
    for (const v of distribution) {
      const idx = Math.min(nBins - 1, Math.max(0, Math.floor((v - lo) / width)));
      bins[idx]++;
    }
    const actualBinIdx = Math.min(
      nBins - 1,
      Math.max(0, Math.floor((actualIc - lo) / width)),
    );

    // Highlight the bin that contains the actual IC.
    const bgColors = bins.map((_, i) =>
      i === actualBinIdx ? 'rgba(234, 88, 12, 0.85)' : 'rgba(100, 116, 139, 0.5)',
    );
    const borderColors = bins.map((_, i) =>
      i === actualBinIdx ? '#ea580c' : '#64748b',
    );

    this.baselineHistChart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: binCenters.map(c => c.toFixed(3)),
        datasets: [
          {
            label: 'Random IC count',
            data: bins,
            backgroundColor: bgColors,
            borderColor: borderColors,
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
            text: `Random-shuffle IC distribution — actual IC = ${actualIc.toFixed(4)} (orange bin)`,
            font: { size: 14, weight: 'bold' },
            color: '#1e293b',
            padding: { bottom: 12 },
          },
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1e293b',
            callbacks: {
              label: ctx =>
                `Bin center ${binCenters[ctx.dataIndex].toFixed(4)}: ${ctx.raw} sims`,
            },
          },
        },
        scales: {
          y: {
            title: { display: true, text: 'Count', color: '#475569' },
            ticks: { precision: 0, color: '#64748b' },
            grid: { color: '#f1f5f9' },
          },
          x: {
            title: { display: true, text: 'IC bin', color: '#475569' },
            ticks: { color: '#64748b', maxRotation: 45, maxTicksLimit: 10 },
            grid: { display: false },
          },
        },
      },
    });
  }
}
