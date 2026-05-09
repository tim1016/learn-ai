import {
  Component,
  signal,
  computed,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import {
  SignalEngineResult,
  SignalBacktestResult,
  WalkForwardResult,
  WalkForwardWindow,
  AlphaDecayStats,
  GraduationResult,
  GraduationCriterion,
  ParameterStability,
  ThresholdSharpeEntry,
  Stage0Rejection,
  Stage0Failure,
  GraduationStageInfo,
  StageAdvanceCriterion,
  SignalDiagnostics,
  DataSufficiency,
  RegimeCoverageEntry,
  EffectiveSampleSize,
  SignalBehaviorMetrics,
  SharpeCi,
  DeflatedSharpe,
  RegimeBucket,
  Methodology,
} from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { SignalReportComponent } from '../signal-report/signal-report.component';
import { RunProgressPanelComponent, PhaseRailStop } from '../shared/run-progress-panel/run-progress-panel.component';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ToggleSwitch } from 'primeng/toggleswitch';
import { TagModule } from 'primeng/tag';
import { IndicatorCatalogComponent } from '../../../shared/indicator-catalog/indicator-catalog.component';
import {
  IndicatorCatalogService,
  IndicatorInfo,
} from '../../../shared/indicator-catalog/indicator-catalog.service';
import { findFeatureId } from '../../../shared/indicator-catalog/feature-mapping';
import { ActiveIndicatorCardComponent } from '../../data-lab/active-indicator-card/active-indicator-card.component';
import { IndicatorConfigModalComponent } from '../../data-lab/indicator-config-modal/indicator-config-modal.component';
import { PolygonDateRangeComponent } from '../../../shared/polygon-date-range';

/** Snake-case shape returned by /api/jobs-internal/signal-engine worker. */
interface SignalEngineJobResultRaw {
  success: boolean;
  ticker: string;
  feature_name: string;
  start_date: string;
  end_date: string;
  bars_used: number;
  flip_sign: boolean;
  thresholds_tested: number[];
  cost_bps_options: number[];
  best_threshold: number;
  best_cost_bps: number;
  backtest_grid: unknown[];
  walk_forward: unknown;
  graduation: unknown;
  signal_diagnostics: unknown;
  data_sufficiency: unknown;
  effective_sample: unknown;
  regime_coverage: Record<string, number>;
  joint_regime_coverage: unknown[];
  signal_behavior: unknown;
  oos_sharpe_ci: unknown;
  deflated_sharpe: unknown;
  methodology: unknown;
  research_log: string;
  error: string | null;
}

@Component({
  selector: 'app-signal-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    SignalReportComponent,
    RunProgressPanelComponent,
    Select,
    InputText,
    ButtonModule,
    MessageModule,
    ToggleSwitch,
    TagModule,
    IndicatorCatalogComponent,
    ActiveIndicatorCardComponent,
    IndicatorConfigModalComponent,
    PolygonDateRangeComponent,
  ],
  templateUrl: './signal-runner.component.html',
  styleUrls: ['./signal-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalRunnerComponent {
  protected readonly signalEngineRail: readonly PhaseRailStop[] = [
    { label: 'Fetch',        ids: ['loading_bars'] },
    { label: 'Walk-forward', ids: ['compute_feature', 'diagnostics', 'regime_coverage', 'backtest_grid', 'walk_forward'] },
    { label: 'Stability',    ids: ['effective_sample'] },
    { label: 'Graduation',   ids: ['graduation'] },
  ];

  private jobsService = inject(JobsService);
  private destroyRef = inject(DestroyRef);
  private catalog = inject(IndicatorCatalogService);

  // Form inputs
  ticker = signal('AAPL');
  /** Catalog-driven selection: indicator key + params (matches data-lab's
   *  ``IndicatorEntry`` shape). The ``feature_name`` sent to the backend is
   *  derived from this via ``findFeatureId``. */
  selectedIndicator = signal<string>('rsi');
  selectedParams = signal<Record<string, number>>({ length: 14 });
  fromDate = signal('2024-01-01');
  toDate = signal('2024-06-30');
  flipSign = signal(true);
  regimeGateEnabled = signal(true);
  forceRefresh = signal(false);

  /** Selected-name set fed to the catalog (single element, single-select). */
  readonly selectedNames = computed<ReadonlySet<string>>(
    () => new Set([this.selectedIndicator()]),
  );

  readonly activeEntry = computed(() => ({
    name: this.selectedIndicator(),
    params: this.selectedParams(),
  }));

  readonly activeParamConfigs = computed(
    () => this.catalog.get(this.selectedIndicator())?.configurable_params ?? [],
  );

  readonly featureMapping = computed(() =>
    findFeatureId(this.selectedIndicator(), this.selectedParams()),
  );

  readonly featureName = computed<string>(
    () => this.featureMapping()?.featureId ?? '',
  );

  readonly configureModalOpen = signal<boolean>(false);

  readonly unsupportedReason = computed<string | null>(() => {
    if (this.featureMapping() !== null) return null;
    const ind = this.selectedIndicator();
    return `${ind.toUpperCase()} with the current parameters isn't backend-supported as a feature yet. Try RSI(14), MACD(12/26/9), or MOM(5).`;
  });

  // Run state
  readonly jobId = signal<string | null>(null);
  readonly result = signal<SignalEngineResult | null>(null);
  readonly error = signal<string | null>(null);
  readonly formCollapsed = signal(false);

  /** Rolling FIFO log buffer (cap 500). */
  readonly logBuffer = new RunLogBuffer();
  readonly logEntries = this.logBuffer.entries;

  readonly job = computed<JobState | null>(() => {
    const id = this.jobId();
    if (!id) return null;
    return this.jobsService.job(id) ?? null;
  });

  readonly loading = computed<boolean>(() => {
    const j = this.job();
    return j !== null && (j.status === 'queued' || j.status === 'running');
  });

  readonly cached = computed<boolean>(() => this.job()?.cached === true);

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.featureMapping() !== null &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
      !this.loading()
    );
  });

  get selectedFeatureLabel(): string {
    return this.featureMapping()?.label ?? this.selectedIndicator().toUpperCase();
  }

  get runSummary(): string {
    return `${this.selectedFeatureLabel} on ${this.ticker().toUpperCase()} (${this.fromDate()} to ${this.toDate()})`;
  }

  /** Catalog click in single-select mode → replace the active selection. */
  onCatalogSelect(ind: IndicatorInfo): void {
    this.selectedIndicator.set(ind.name);
    this.selectedParams.set(this.catalog.defaultParams(ind.name));
  }

  openConfigure(): void {
    this.configureModalOpen.set(true);
  }

  closeConfigure(open: boolean): void {
    if (!open) this.configureModalOpen.set(false);
  }

  onModalParamChange(change: { name: string; value: number }): void {
    this.selectedParams.update((p) => ({ ...p, [change.name]: change.value }));
  }

  resetSelectedToDefaults(): void {
    this.selectedParams.set(this.catalog.defaultParams(this.selectedIndicator()));
  }

  resetSelectedParam(paramName: string): void {
    const def = this.catalog
      .get(this.selectedIndicator())
      ?.configurable_params.find((p) => p.name === paramName)?.default;
    if (typeof def === 'number') {
      this.selectedParams.update((p) => ({ ...p, [paramName]: def }));
    }
  }

  constructor() {
    let lastLogSeq = -1;
    let resultFetchedFor: string | null = null;

    effect(() => {
      this.jobsService.jobs(); // dependency
      const id = this.jobId();
      if (!id) return;
      const j = this.jobsService.job(id);
      if (!j) return;

      // Dedupe by monotonic ``seq`` rather than ``ts`` — two SSE log
      // events sharing a wall-clock millisecond would otherwise collapse
      // and the dropped line would never reappear once it slides past
      // the 5-entry recentLogs window.
      for (const log of j.recentLogs) {
        if (log.seq > lastLogSeq) {
          const level = pythonLevelToEntryLevel(log.level);
          this.logBuffer.append(level, glyphForLevel(level), log.message);
          lastLogSeq = log.seq;
        }
      }

      if (j.status === 'completed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        void this.handleCompleted(id);
      } else if (j.status === 'failed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.errorMessage ?? 'Signal engine run failed');
      } else if (j.status === 'cancelled' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.message ?? 'Run cancelled');
      }
    });
  }

  async runSignalEngine(): Promise<void> {
    this.error.set(null);
    this.result.set(null);
    this.logBuffer.clear();

    try {
      const featureId = this.featureName();
      if (!featureId) {
        this.error.set(this.unsupportedReason() ?? 'No feature selected');
        return;
      }
      const id = await this.jobsService.startJob('signal_engine', {
        ticker: this.ticker().toUpperCase(),
        feature_name: featureId,
        from_date: this.fromDate(),
        to_date: this.toDate(),
        flip_sign: this.flipSign(),
        regime_gate_enabled: this.regimeGateEnabled(),
        force: this.forceRefresh(),
      });
      this.jobId.set(id);
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to start signal engine run';
      this.error.set(msg);
    }
  }

  async cancelRun(): Promise<void> {
    const id = this.jobId();
    if (!id) return;
    try {
      await this.jobsService.cancelJob(id);
    } catch (err: unknown) {
      // Tolerate the race where the worker terminated before cancel
      // landed — the terminal SSE event makes the cancel moot. Surface
      // every other failure so a real network or server error doesn't
      // leave the UI silently stuck on "running" (matches batch-runner).
      const status = this.jobsService.job(id)?.status;
      if (status === 'completed' || status === 'cancelled' || status === 'failed') {
        return;
      }
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to cancel signal engine run';
      this.error.set(msg);
    }
  }

  toggleForm(): void {
    this.formCollapsed.update(v => !v);
  }

  newRun(): void {
    const id = this.jobId();
    if (id) {
      this.jobsService.dismiss(id);
    }
    this.jobId.set(null);
    this.result.set(null);
    this.error.set(null);
    this.formCollapsed.set(false);
    this.logBuffer.clear();
  }

  private async handleCompleted(id: string): Promise<void> {
    try {
      const raw = await this.jobsService.fetchResult<SignalEngineJobResultRaw>(id);
      const mapped = this.toSignalEngineResult(raw);
      this.result.set(mapped);
      if (mapped.success) {
        this.formCollapsed.set(true);
      } else if (mapped.error) {
        this.error.set(mapped.error);
      }
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to fetch signal engine result';
      this.error.set(msg);
    }
  }

  /** Transform snake_case worker payload → camelCase SignalEngineResult. */
  private toSignalEngineResult(raw: SignalEngineJobResultRaw): SignalEngineResult {
    const regimeCoverageEntries = Object.entries(raw.regime_coverage ?? {}).map(
      ([regime, count]) => ({ regime, count }),
    );
    return {
      success: raw.success,
      ticker: raw.ticker,
      featureName: raw.feature_name,
      startDate: raw.start_date,
      endDate: raw.end_date,
      barsUsed: raw.bars_used,
      flipSign: raw.flip_sign,
      thresholdsTested: raw.thresholds_tested ?? [],
      costBpsOptions: raw.cost_bps_options ?? [],
      bestThreshold: raw.best_threshold,
      bestCostBps: raw.best_cost_bps,
      backtestGrid: this.mapBacktestGrid(raw.backtest_grid),
      walkForward: raw.walk_forward
        ? this.mapWalkForward(raw.walk_forward as Record<string, unknown>)
        : null,
      graduation: raw.graduation
        ? this.mapGraduation(raw.graduation as Record<string, unknown>)
        : null,
      signalDiagnostics: raw.signal_diagnostics
        ? this.mapSignalDiagnostics(raw.signal_diagnostics as Record<string, unknown>)
        : null,
      dataSufficiency: raw.data_sufficiency
        ? this.mapDataSufficiency(raw.data_sufficiency as Record<string, unknown>)
        : null,
      effectiveSample: raw.effective_sample
        ? this.mapEffectiveSample(raw.effective_sample as Record<string, unknown>)
        : null,
      regimeCoverage: regimeCoverageEntries,
      jointRegimeCoverage: this.mapJointRegimeCoverage(raw.joint_regime_coverage),
      signalBehavior: raw.signal_behavior
        ? this.mapSignalBehavior(raw.signal_behavior as Record<string, unknown>)
        : null,
      oosSharpeCi: raw.oos_sharpe_ci
        ? this.mapSharpeCi(raw.oos_sharpe_ci as Record<string, unknown>)
        : null,
      deflatedSharpe: raw.deflated_sharpe
        ? this.mapDeflatedSharpe(raw.deflated_sharpe as Record<string, unknown>)
        : null,
      methodology: raw.methodology
        ? this.mapMethodology(raw.methodology as Record<string, unknown>)
        : null,
      researchLog: raw.research_log ?? '',
      error: raw.error ?? undefined,
    };
  }

  private mapBacktestGrid(raw: unknown[]): SignalBacktestResult[] {
    if (!Array.isArray(raw)) return [];
    return (raw as Record<string, unknown>[]).map(b => ({
      threshold: b['threshold'] as number,
      costBps: b['cost_bps'] as number,
      dates: (b['dates'] as string[]) ?? [],
      cumulativeReturns: (b['cumulative_returns'] as number[]) ?? [],
      positions: (b['positions'] as number[]) ?? [],
      grossSharpe: b['gross_sharpe'] as number,
      netSharpe: b['net_sharpe'] as number,
      maxDrawdown: b['max_drawdown'] as number,
      annualizedTurnover: b['annualized_turnover'] as number,
      avgHoldingBars: b['avg_holding_bars'] as number,
      winRate: b['win_rate'] as number,
      avgWinLossRatio: b['avg_win_loss_ratio'] as number,
      totalTrades: b['total_trades'] as number,
      netTotalReturn: b['net_total_return'] as number,
      grossTotalReturn: b['gross_total_return'] as number,
    }));
  }

  private mapAlphaDecay(r: Record<string, unknown>): AlphaDecayStats {
    return {
      slope: r['slope'] as number,
      intercept: r['intercept'] as number,
      tStat: r['t_stat'] as number,
      pValue: r['p_value'] as number,
      rSquared: r['r_squared'] as number,
      nFoldsUsed: r['n_folds_used'] as number,
      isTestValid: r['is_test_valid'] as boolean,
      isSignificant: r['is_significant'] as boolean,
    };
  }

  private mapWalkForwardWindow(r: Record<string, unknown>): WalkForwardWindow {
    return {
      foldIndex: r['fold_index'] as number,
      trainStart: r['train_start'] as string,
      trainEnd: r['train_end'] as string,
      testStart: r['test_start'] as string,
      testEnd: r['test_end'] as string,
      trainBars: r['train_bars'] as number,
      testBars: r['test_bars'] as number,
      mu: r['mu'] as number,
      sigma: r['sigma'] as number,
      bestThreshold: r['best_threshold'] as number,
      oosNetSharpe: r['oos_net_sharpe'] as number,
      oosGrossSharpe: r['oos_gross_sharpe'] as number,
      oosMaxDrawdown: r['oos_max_drawdown'] as number,
      oosNetReturn: r['oos_net_return'] as number,
      oosWinRate: r['oos_win_rate'] as number,
      oosTotalTrades: r['oos_total_trades'] as number,
      oosDates: (r['oos_dates'] as string[]) ?? [],
      oosCumulativeReturns: (r['oos_cumulative_returns'] as number[]) ?? [],
    };
  }

  private mapWalkForward(r: Record<string, unknown>): WalkForwardResult {
    const windows = (r['windows'] as Record<string, unknown>[]) ?? [];
    const ad = r['alpha_decay'] as Record<string, unknown> | null;
    return {
      windows: windows.map(w => this.mapWalkForwardWindow(w)),
      meanOosSharpe: r['mean_oos_sharpe'] as number,
      stdOosSharpe: r['std_oos_sharpe'] as number,
      medianOosSharpe: r['median_oos_sharpe'] as number,
      pctWindowsProfitable: r['pct_windows_profitable'] as number,
      pctWindowsPositiveSharpe: r['pct_windows_positive_sharpe'] as number,
      worstWindowSharpe: r['worst_window_sharpe'] as number,
      bestWindowSharpe: r['best_window_sharpe'] as number,
      totalOosBars: r['total_oos_bars'] as number,
      combinedOosDates: (r['combined_oos_dates'] as string[]) ?? [],
      combinedOosCumulativeReturns: (r['combined_oos_cumulative_returns'] as number[]) ?? [],
      oosSharpeTrendSlope: r['oos_sharpe_trend_slope'] as number,
      alphaDecay: ad ? this.mapAlphaDecay(ad) : null,
    };
  }

  private mapGraduationCriterion(r: Record<string, unknown>): GraduationCriterion {
    return {
      name: r['name'] as string,
      description: r['description'] as string,
      passed: r['passed'] as boolean,
      value: r['value'] as number,
      threshold: r['threshold'] as number,
      label: r['label'] as string,
      failureReason: r['failure_reason'] as string,
    };
  }

  private mapStageAdvanceCriterion(r: Record<string, unknown>): StageAdvanceCriterion {
    return {
      name: r['name'] as string,
      description: r['description'] as string,
      currentValue: r['current_value'] as number,
      requiredRepr: r['required_repr'] as string,
      met: r['met'] as boolean,
    };
  }

  private mapGraduation(r: Record<string, unknown>): GraduationResult {
    const criteria = (r['criteria'] as Record<string, unknown>[]) ?? [];
    const ps = r['parameter_stability'] as Record<string, unknown> | null;
    const s0 = r['stage0_rejection'] as Record<string, unknown> | null;
    const si = r['stage_info'] as Record<string, unknown> | null;

    const parameterStability: ParameterStability | null = ps
      ? {
          sharpeValuesByThreshold: Object.entries(
            (ps['sharpe_values_by_threshold'] as Record<string, number>) ?? {},
          ).map(([threshold, sharpe]): ThresholdSharpeEntry => ({ threshold: Number(threshold), sharpe })),
          stabilityScore: ps['stability_score'] as number,
          stabilityLabel: ps['stability_label'] as string,
        }
      : null;

    const stage0Rejection: Stage0Rejection | null = s0
      ? {
          rejected: s0['rejected'] as boolean,
          failedCriteria: ((s0['failed_criteria'] as Record<string, unknown>[]) ?? []).map(
            (f): Stage0Failure => ({
              criterionName: f['criterion_name'] as string,
              value: f['value'] as number,
              thresholdRepr: f['threshold_repr'] as string,
              message: f['message'] as string,
            }),
          ),
        }
      : null;

    const stageInfo: GraduationStageInfo | null = si
      ? {
          stage: si['stage'] as 0 | 1 | 2 | 3,
          label: si['label'] as string,
          description: si['description'] as string,
          nextStageLabel: si['next_stage_label'] as string,
          advanceCriteria: ((si['advance_criteria'] as Record<string, unknown>[]) ?? []).map(
            c => this.mapStageAdvanceCriterion(c),
          ),
        }
      : null;

    return {
      criteria: criteria.map(c => this.mapGraduationCriterion(c)),
      overallPassed: r['overall_passed'] as boolean,
      overallGrade: r['overall_grade'] as string,
      summary: r['summary'] as string,
      statusLabel: r['status_label'] as string,
      parameterStability,
      stage0Rejection,
      stageInfo,
    };
  }

  private mapSignalDiagnostics(r: Record<string, unknown>): SignalDiagnostics {
    return {
      signalMean: r['signal_mean'] as number,
      signalStd: r['signal_std'] as number,
      pctTimeActive: r['pct_time_active'] as number,
      avgAbsSignal: r['avg_abs_signal'] as number,
      pctFilteredByThreshold: r['pct_filtered_by_threshold'] as number,
      pctGatedByRegime: r['pct_gated_by_regime'] as number,
    };
  }

  private mapDataSufficiency(r: Record<string, unknown>): DataSufficiency {
    const rc = (r['regime_coverage'] as Record<string, unknown>[]) ?? [];
    return {
      totalBars: r['total_bars'] as number,
      trainBars: r['train_bars'] as number,
      testBars: r['test_bars'] as number,
      walkForwardFolds: r['walk_forward_folds'] as number,
      effectiveOosBars: r['effective_oos_bars'] as number,
      regimesCovered: r['regimes_covered'] as number,
      regimeCoverage: rc.map((e): RegimeCoverageEntry => ({
        regime: e['regime'] as string,
        count: e['count'] as number,
      })),
      coverageWarnings: (r['coverage_warnings'] as string[]) ?? [],
    };
  }

  private mapEffectiveSample(r: Record<string, unknown>): EffectiveSampleSize {
    return {
      rawN: r['raw_n'] as number,
      effectiveN: r['effective_n'] as number,
      autocorrelationLag1: r['autocorrelation_lag1'] as number,
      independentBets: r['independent_bets'] as number,
      maxLagUsed: r['max_lag_used'] as number,
      rhoSum: r['rho_sum'] as number,
    };
  }

  private mapJointRegimeCoverage(raw: unknown[]): RegimeBucket[] {
    if (!Array.isArray(raw)) return [];
    return (raw as Record<string, unknown>[]).map(b => ({
      volLabel: b['vol_label'] as string,
      trendLabel: b['trend_label'] as string,
      days: b['days'] as number,
      effectiveTrades: b['effective_trades'] as number,
      badge: b['badge'] as string,
    }));
  }

  private mapSignalBehavior(r: Record<string, unknown>): SignalBehaviorMetrics {
    return {
      avgForwardReturnWhenActive: r['avg_forward_return_when_active'] as number,
      skewnessActiveReturns: r['skewness_active_returns'] as number,
      avgWinReturn: r['avg_win_return'] as number,
      avgLossReturn: r['avg_loss_return'] as number,
      hitRate: r['hit_rate'] as number,
    };
  }

  private mapSharpeCi(r: Record<string, unknown>): SharpeCi {
    return {
      point: r['point'] as number,
      se: r['se'] as number,
      ciLower: r['ci_lower'] as number,
      ciUpper: r['ci_upper'] as number,
      confidenceLevel: r['confidence_level'] as number,
      nEffUsed: r['n_eff_used'] as number,
      valid: r['valid'] as boolean,
    };
  }

  private mapDeflatedSharpe(r: Record<string, unknown>): DeflatedSharpe {
    return {
      rawSharpe: r['raw_sharpe'] as number,
      expectedMaxUnderNull: r['expected_max_under_null'] as number,
      dsrProbability: r['dsr_probability'] as number,
      nTrials: r['n_trials'] as number,
      skewness: r['skewness'] as number,
      kurtosis: r['kurtosis'] as number,
      valid: r['valid'] as boolean,
    };
  }

  private mapMethodology(r: Record<string, unknown>): Methodology {
    return {
      trainMonths: r['train_months'] as number,
      testMonths: r['test_months'] as number,
      windowType: r['window_type'] as string,
      optimizationTarget: r['optimization_target'] as string,
      annualizationFactor: r['annualization_factor'] as number,
      barsPerDay: r['bars_per_day'] as number,
      horizon: r['horizon'] as number,
      defaultCostBps: r['default_cost_bps'] as number,
      minBarsForSignal: r['min_bars_for_signal'] as number,
      flipSign: r['flip_sign'] as boolean,
      regimeGateEnabled: r['regime_gate_enabled'] as boolean,
      thresholds: (r['thresholds'] as number[] | null) ?? null,
      costBpsOptions: (r['cost_bps_options'] as number[] | null) ?? null,
    };
  }
}
