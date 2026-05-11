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
  ResearchResult,
  QuantileBin,
  Robustness,
  MonthlyICBreakdown,
  RollingTStatPoint,
  RegimeIC,
  TrainTestSplit,
  StructuralBreakPoint,
  FeatureValidationSpec,
  FeatureValidationVerdict,
  ValidationScreen,
  MultipleTestingWarning,
  CostViability,
  IcCi,
  FeatureStageInfo,
  FeatureStageCriterion,
} from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { FeatureReportComponent } from '../feature-report/feature-report.component';
import { RunProgressPanelComponent } from '../shared/run-progress-panel/run-progress-panel.component';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { CheckboxModule } from 'primeng/checkbox';
import { TagModule } from 'primeng/tag';
import {
  IndicatorPickerAdd,
  IndicatorPickerComponent,
} from '../../../shared/indicator-picker/indicator-picker.component';
import {
  IndicatorCatalogService,
} from '../../../shared/indicator-catalog/indicator-catalog.service';
import { findFeatureId } from '../../../shared/indicator-catalog/feature-mapping';
import { ActiveIndicatorCardComponent } from '../../data-lab/active-indicator-card/active-indicator-card.component';
import { IndicatorConfigModalComponent } from '../../data-lab/indicator-config-modal/indicator-config-modal.component';
import { TickerRangePickerComponent } from '../../../shared/ticker-range-picker/ticker-range-picker.component';
import type {
  Resolution,
  TickerRange,
} from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import { TICKER_POOL, RECENT_TICKERS } from '../../../shared/ticker-catalog';
import { tickerRangeToWire } from '../../../utils/ticker-wire';

/** Shape of the ``target`` payload emitted by ``_serialize_target`` in
 *  ``PythonDataService/app/routers/jobs.py``. Mirrors the GraphQL
 *  ``TargetMetadata`` so the mapper below can rename keys 1:1. */
interface FeatureResearchTargetRaw {
  target_name: string;
  horizon_minutes: number;
  horizon_bars: number;
  bar_minutes: number;
  timezone: string;
  valid_count: number;
  total_count: number;
  valid_ratio: number;
  invalid_reason_counts: { reason: string; count: number }[];
}

/** Snake-case shape returned by /api/jobs-internal/feature-research worker. */
interface FeatureResearchJobResultRaw {
  success: boolean;
  ticker: string;
  feature_name: string;
  start_date: string;
  end_date: string;
  bars_used: number;
  mean_ic: number;
  ic_t_stat: number;
  ic_p_value: number;
  nw_t_stat: number;
  nw_p_value: number;
  effective_n: number;
  ic_values: number[];
  ic_dates: string[];
  adf_pvalue: number;
  kpss_pvalue: number;
  is_stationary: boolean;
  quantile_bins: unknown[];
  is_monotonic: boolean;
  monotonicity_ratio: number;
  robustness: unknown;
  feature_spec: unknown;
  validation_verdict: unknown;
  target?: FeatureResearchTargetRaw | null;
  passed_validation: boolean;
  error: string | null;
}

@Component({
  selector: 'app-feature-runner',
  imports: [
    CommonModule,
    FormsModule,
    FeatureReportComponent,
    RunProgressPanelComponent,
    ButtonModule,
    MessageModule,
    CheckboxModule,
    TagModule,
    IndicatorPickerComponent,
    ActiveIndicatorCardComponent,
    IndicatorConfigModalComponent,
    TickerRangePickerComponent,
  ],
  templateUrl: './feature-runner.component.html',
  styleUrls: ['./feature-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeatureRunnerComponent {
  private jobsService = inject(JobsService);
  private destroyRef = inject(DestroyRef);
  private catalog = inject(IndicatorCatalogService);

  /** Catalog passed to the picker. Exposed as a getter so the template can
   *  read the service signal directly without leaking the service. */
  readonly catalogCategories = this.catalog.categories;
  readonly catalogLoading = this.catalog.loading;

  // Form inputs
  // Single TickerRange replaces (ticker, fromDate, toDate, timespan,
  // multiplier). multiplier=1 matches FeatureResearchJobRequest's
  // pre-migration default; the base TickerRequest also defaults to 1
  // so no behavior change.
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: '2024-01-01',
    to: '2024-03-31',
    resolution: 'minute',
    multiplier: 1,
  });
  readonly tickerPool = TICKER_POOL;
  readonly recentTickers = RECENT_TICKERS;
  readonly availableMultipliers: readonly number[] = [1, 5, 15, 60, 240];
  readonly availableResolutions: readonly Resolution[] = ['minute', 'hour', 'daily'];

  /** Catalog-driven selection: indicator key + params (matches data-lab's
   *  ``IndicatorEntry`` shape). The ``featureName`` sent to the backend is
   *  derived from this via ``findFeatureId`` so the IC test still routes
   *  through the existing fixed-ID feature_research worker. */
  selectedIndicator = signal<string>('rsi');
  selectedParams = signal<Record<string, number>>({ length: 14 });
  forceRun = signal(false);

  /** Single-element activeKeys array fed to the picker so the chosen row
   *  shows the active rail + +1 badge. */
  readonly pickerActiveKeys = computed<readonly string[]>(
    () => [this.selectedIndicator()],
  );

  /** Synthetic active-indicator entry rendered on the right of the workspace. */
  readonly activeEntry = computed(() => ({
    name: this.selectedIndicator(),
    params: this.selectedParams(),
  }));

  /** Param schema for the selected indicator (drives the config modal). */
  readonly activeParamConfigs = computed(
    () => this.catalog.get(this.selectedIndicator())?.configurable_params ?? [],
  );

  /** Backend feature mapping for the current pick — null when the combo
   *  isn't backend-supported yet. */
  readonly featureMapping = computed(() =>
    findFeatureId(this.selectedIndicator(), this.selectedParams()),
  );

  /** Computed string ID for the worker payload (e.g. ``rsi_14``). */
  readonly featureName = computed<string>(
    () => this.featureMapping()?.featureId ?? '',
  );

  /** Right-anchored config modal state. */
  readonly configureModalOpen = signal<boolean>(false);

  // Run state
  readonly jobId = signal<string | null>(null);
  readonly result = signal<ResearchResult | null>(null);
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

  // ── Range guard ─────────────────────────────────────
  // Minute-resolution research at 2 years exceeds ~200k bars, which overruns
  // the backend→python keep-alive window. Cap minute studies to 180 calendar
  // days; hour at 3 years; day unrestricted.
  private readonly MAX_DAYS_BY_RESOLUTION: Readonly<Record<Resolution, number>> = {
    minute: 180,
    hour: 1095,
    daily: Number.POSITIVE_INFINITY,
  };

  readonly maxRangeDays = computed<number>(
    () => this.MAX_DAYS_BY_RESOLUTION[this.range().resolution] ?? 180,
  );

  readonly rangeDays = computed<number | null>(() => {
    const r = this.range();
    const from = Date.parse(r.from);
    const to = Date.parse(r.to);
    if (Number.isNaN(from) || Number.isNaN(to) || to < from) return null;
    return Math.round((to - from) / 86_400_000);
  });

  readonly rangeWarning = computed<string | null>(() => {
    const days = this.rangeDays();
    if (days === null) return null;
    const cap = this.maxRangeDays();
    if (days <= cap) return null;
    const capLabel = Number.isFinite(cap) ? `${cap} days` : 'no cap';
    return `${this.range().resolution}-resolution research is capped at ${capLabel} (you selected ${days} days). Long minute-bar requests don't survive the backend→python connection window. Shorten the range, or switch to an hourly/daily resolution.`;
  });

  canRun = computed(() => {
    const r = this.range();
    return (
      r.symbol.trim().length > 0 &&
      this.featureMapping() !== null &&
      r.from.trim().length > 0 &&
      r.to.trim().length > 0 &&
      this.rangeWarning() === null &&
      !this.loading()
    );
  });

  /** Block reason shown next to a disabled run button when the user has
   *  picked a catalog indicator the backend can't compute as a feature yet. */
  readonly unsupportedReason = computed<string | null>(() => {
    if (this.featureMapping() !== null) return null;
    const ind = this.selectedIndicator();
    return `${ind.toUpperCase()} with the current parameters isn't backend-supported as a feature yet. Try RSI(14), MACD(12/26/9), or MOM(5).`;
  });

  get selectedFeatureLabel(): string {
    return this.featureMapping()?.label ?? this.selectedIndicator().toUpperCase();
  }

  get runSummary(): string {
    const r = this.range();
    return `${this.selectedFeatureLabel} on ${r.symbol.toUpperCase()} (${r.from} to ${r.to})`;
  }

  /** Picker (add) in this host = single-select replace. The picker fires
   *  (add) on every row click; we treat each fire as "set this as the
   *  active feature" rather than accumulating. */
  onPickerSelect(event: IndicatorPickerAdd): void {
    this.selectedIndicator.set(event.name);
    // The picker passes catalog-default params already; mirror to local
    // state so the active-card and config-modal see them.
    this.selectedParams.set({ ...event.params });
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

  // Tracks which job id we've already settled (fetched result, surfaced
  // error, or acknowledged cancellation) so the effect doesn't re-fire
  // its terminal branch on every subsequent event tick. ``handleCompleted``
  // sets this *after* the fetch succeeds so a transient ``fetchResult``
  // failure doesn't lock out retries.
  private resultSettledFor: string | null = null;

  constructor() {
    // The picker is purely a renderer — it doesn't fetch the catalog itself.
    // The old IndicatorCatalogComponent triggered this implicitly.
    void this.catalog.load();

    let lastLogSeq = -1;

    effect(() => {
      this.jobsService.jobs(); // dependency
      const id = this.jobId();
      if (!id) return;
      const j = this.jobsService.job(id);
      if (!j) return;

      // Forward log lines into the rolling buffer. Dedupe by the
      // monotonic ``seq`` rather than ``ts`` so two events landing in
      // the same millisecond aren't collapsed into one — the second
      // line would otherwise be lost once it slides past the 5-entry
      // ``recentLogs`` window.
      for (const log of j.recentLogs) {
        if (log.seq > lastLogSeq) {
          const level = pythonLevelToEntryLevel(log.level);
          this.logBuffer.append(level, glyphForLevel(level), log.message);
          lastLogSeq = log.seq;
        }
      }

      if (j.status === 'completed' && this.resultSettledFor !== id) {
        void this.handleCompleted(id);
      } else if (j.status === 'failed' && this.resultSettledFor !== id) {
        this.resultSettledFor = id;
        this.error.set(j.errorMessage ?? 'Feature research run failed');
      } else if (j.status === 'cancelled' && this.resultSettledFor !== id) {
        this.resultSettledFor = id;
        this.error.set(j.message ?? 'Run cancelled');
      }
    });
  }

  async runResearch(): Promise<void> {
    this.error.set(null);
    this.result.set(null);
    this.logBuffer.clear();

    try {
      const featureId = this.featureName();
      if (!featureId) {
        this.error.set(this.unsupportedReason() ?? 'No feature selected');
        return;
      }
      // Wire the canonical TickerRange shape via the shared adapter.
      // Python's FeatureResearchJobRequest inherits TickerRequest in PR
      // (ii); legacy aliases are still accepted until PR (iii)'s
      // alias-removal commit.
      const wire = tickerRangeToWire({
        ...this.range(),
        symbol: this.range().symbol.toUpperCase(),
      });
      const id = await this.jobsService.startJob('feature_research', {
        ...wire,
        feature_name: featureId,
        force: this.forceRun(),
      });
      this.jobId.set(id);
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to start feature research run';
      this.error.set(msg);
    }
  }

  async cancelRun(): Promise<void> {
    const id = this.jobId();
    if (!id) return;
    try {
      await this.jobsService.cancelJob(id);
    } catch (err: unknown) {
      // Tolerate the race where the worker terminated before the cancel
      // request landed — the terminal SSE event makes the cancel moot.
      // Surface every other failure so a real network or server error
      // doesn't leave the UI silently stuck on "running" (matches the
      // batch-runner cancel handler).
      const status = this.jobsService.job(id)?.status;
      if (status === 'completed' || status === 'cancelled' || status === 'failed') {
        return;
      }
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to cancel feature research run';
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
      const raw = await this.jobsService.fetchResult<FeatureResearchJobResultRaw>(id);
      const mapped = this.toResearchResult(raw);
      this.result.set(mapped);
      if (mapped.success) {
        this.formCollapsed.set(true);
      } else if (mapped.error) {
        this.error.set(mapped.error);
      }
      // Only mark this id as settled once the fetch actually returned;
      // a transient ``fetchResult`` failure should leave the guard open
      // so the next event tick can retry.
      this.resultSettledFor = id;
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to fetch research result';
      this.error.set(msg);
    }
  }

  /** Map snake_case worker payload → camelCase ResearchResult. */
  private toResearchResult(raw: FeatureResearchJobResultRaw): ResearchResult {
    return {
      success: raw.success,
      ticker: raw.ticker,
      featureName: raw.feature_name,
      startDate: raw.start_date,
      endDate: raw.end_date,
      barsUsed: raw.bars_used,
      meanIC: raw.mean_ic,
      icTStat: raw.ic_t_stat,
      icPValue: raw.ic_p_value,
      nwTStat: raw.nw_t_stat,
      nwPValue: raw.nw_p_value,
      effectiveN: raw.effective_n,
      icValues: raw.ic_values ?? [],
      icDates: raw.ic_dates ?? [],
      adfPvalue: raw.adf_pvalue,
      kpssPvalue: raw.kpss_pvalue,
      isStationary: raw.is_stationary,
      quantileBins: this.mapQuantileBins(raw.quantile_bins),
      isMonotonic: raw.is_monotonic,
      monotonicityRatio: raw.monotonicity_ratio,
      passedValidation: raw.passed_validation,
      robustness: raw.robustness ? this.mapRobustness(raw.robustness as Record<string, unknown>) : undefined,
      featureSpec: raw.feature_spec ? this.mapFeatureSpec(raw.feature_spec as Record<string, unknown>) : undefined,
      validationVerdict: raw.validation_verdict
        ? this.mapValidationVerdict(raw.validation_verdict as Record<string, unknown>)
        : undefined,
      targetMetadata: raw.target
        ? {
            targetName: raw.target.target_name,
            horizonMinutes: raw.target.horizon_minutes,
            horizonBars: raw.target.horizon_bars,
            barMinutes: raw.target.bar_minutes,
            timezone: raw.target.timezone,
            validCount: raw.target.valid_count,
            totalCount: raw.target.total_count,
            validRatio: raw.target.valid_ratio,
            invalidReasonCounts: raw.target.invalid_reason_counts,
          }
        : null,
      error: raw.error ?? undefined,
    };
  }

  private mapQuantileBins(raw: unknown[]): QuantileBin[] {
    if (!Array.isArray(raw)) return [];
    return (raw as Record<string, unknown>[]).map(b => ({
      binNumber: b['bin_number'] as number,
      lowerBound: b['lower_bound'] as number,
      upperBound: b['upper_bound'] as number,
      meanReturn: b['mean_return'] as number,
      count: b['count'] as number,
    }));
  }

  private mapRobustness(r: Record<string, unknown>): Robustness {
    const monthly = (r['monthly_breakdown'] as Record<string, unknown>[] | null) ?? [];
    const rolling = (r['rolling_t_stat'] as Record<string, unknown>[] | null) ?? [];
    const volRegimes = (r['volatility_regimes'] as Record<string, unknown>[] | null) ?? [];
    const trendRegimes = (r['trend_regimes'] as Record<string, unknown>[] | null) ?? [];
    const breaks = (r['structural_breaks'] as Record<string, unknown>[] | null) ?? [];
    const tt = r['train_test'] as Record<string, unknown> | null;

    return {
      monthlyBreakdown: monthly.map((m): MonthlyICBreakdown => ({
        month: m['month'] as string,
        meanIC: m['mean_ic'] as number,
        tStat: m['t_stat'] as number,
        observationCount: m['observation_count'] as number,
      })),
      pctPositiveMonths: r['pct_positive_months'] as number,
      pctSignificantMonths: r['pct_significant_months'] as number,
      bestMonthIC: r['best_month_ic'] as number,
      worstMonthIC: r['worst_month_ic'] as number,
      stabilityLabel: r['stability_label'] as string,
      pctSignConsistentMonths: r['pct_sign_consistent_months'] as number,
      signConsistentStabilityLabel: r['sign_consistent_stability_label'] as string,
      rollingTStat: rolling.map((p): RollingTStatPoint => ({
        month: p['month'] as string,
        tStatSmoothed: p['t_stat_smoothed'] as number,
      })),
      volatilityRegimes: volRegimes.map((v): RegimeIC => ({
        regimeLabel: v['regime_label'] as string,
        meanIC: v['mean_ic'] as number,
        tStat: v['t_stat'] as number,
        observationCount: v['observation_count'] as number,
      })),
      trendRegimes: trendRegimes.map((v): RegimeIC => ({
        regimeLabel: v['regime_label'] as string,
        meanIC: v['mean_ic'] as number,
        tStat: v['t_stat'] as number,
        observationCount: v['observation_count'] as number,
      })),
      trainTest: tt
        ? {
            trainStart: tt['train_start'] as string,
            trainEnd: tt['train_end'] as string,
            testStart: tt['test_start'] as string,
            testEnd: tt['test_end'] as string,
            trainMeanIC: tt['train_mean_ic'] as number,
            trainTStat: tt['train_t_stat'] as number,
            trainDays: tt['train_days'] as number,
            testMeanIC: tt['test_mean_ic'] as number,
            testTStat: tt['test_t_stat'] as number,
            testDays: tt['test_days'] as number,
            overfitFlag: tt['overfit_flag'] as boolean,
            oosRetention: tt['oos_retention'] as number,
            oosRetentionLabel: tt['oos_retention_label'] as string,
          } satisfies TrainTestSplit
        : null,
      structuralBreaks: breaks.map((b): StructuralBreakPoint => ({
        date: b['date'] as string,
        icBefore: b['ic_before'] as number,
        icAfter: b['ic_after'] as number,
        tStat: b['t_stat'] as number,
        significant: b['significant'] as boolean,
      })),
    };
  }

  private mapFeatureSpec(r: Record<string, unknown>): FeatureValidationSpec {
    return {
      featureName: r['feature_name'] as string,
      defaultTarget: r['default_target'] as string,
      expectedDirection: r['expected_direction'] as string,
      expectedShape: r['expected_shape'] as string,
      stationarityRequired: r['stationarity_required'] as boolean,
      monotonicityRequired: r['monotonicity_required'] as boolean,
      isSignedTargetAppropriate: r['is_signed_target_appropriate'] as boolean,
      intent: r['intent'] as string,
      notes: (r['notes'] as string[]) ?? [],
    };
  }

  private mapScreen(r: Record<string, unknown>): ValidationScreen {
    return {
      name: r['name'] as string,
      description: r['description'] as string,
      passed: r['passed'] as boolean,
      requiredForStage1: r['required_for_stage1'] as boolean,
      failureReasons: (r['failure_reasons'] as string[]) ?? [],
    };
  }

  private mapValidationVerdict(r: Record<string, unknown>): FeatureValidationVerdict {
    const mt = r['multiple_testing'] as Record<string, unknown>;
    const cv = r['cost_viability'] as Record<string, unknown>;
    const ci = r['ic_ci'] as Record<string, unknown>;
    const si = r['stage_info'] as Record<string, unknown>;
    const criteria = (si['advance_criteria'] as Record<string, unknown>[]) ?? [];

    return {
      statisticalScreen: this.mapScreen(r['statistical_screen'] as Record<string, unknown>),
      economicScreen: this.mapScreen(r['economic_screen'] as Record<string, unknown>),
      oosScreen: this.mapScreen(r['oos_screen'] as Record<string, unknown>),
      multipleTestingScreen: this.mapScreen(r['multiple_testing_screen'] as Record<string, unknown>),
      regimeStabilityScreen: this.mapScreen(r['regime_stability_screen'] as Record<string, unknown>),
      multipleTesting: {
        rawNwPValue: mt['raw_nw_p_value'] as number,
        holmPValue: mt['holm_p_value'] as number,
        nFamily: mt['n_family'] as number,
        note: mt['note'] as string,
      } satisfies MultipleTestingWarning,
      costViability: {
        grossSpreadBpsSigned: cv['gross_spread_bps_signed'] as number,
        directionalSpreadBps: cv['directional_spread_bps'] as number,
        costAssumptionOneWayBps: cv['cost_assumption_one_way_bps'] as number,
        costErasureOneWayBps: cv['cost_erasure_one_way_bps'] as number,
        netSpreadBpsAtAssumption: cv['net_spread_bps_at_assumption'] as number,
        viableAtAssumption: cv['viable_at_assumption'] as boolean,
        specDirection: cv['spec_direction'] as string,
        note: cv['note'] as string,
      } satisfies CostViability,
      icCi: {
        point: ci['point'] as number,
        se: ci['se'] as number,
        ciLower: ci['ci_lower'] as number,
        ciUpper: ci['ci_upper'] as number,
        confidenceLevel: ci['confidence_level'] as number,
        nEffUsed: ci['n_eff_used'] as number,
        valid: ci['valid'] as boolean,
        seApproximationNote: ci['se_approximation_note'] as string,
      } satisfies IcCi,
      directionMatchesSpec: r['direction_matches_spec'] as boolean,
      targetSignedAppropriate: r['target_signed_appropriate'] as boolean,
      stageInfo: {
        stage: si['stage'] as 0 | 1 | 2 | 3,
        label: si['label'] as string,
        description: si['description'] as string,
        nextStageLabel: si['next_stage_label'] as string,
        advanceCriteria: criteria.map((c): FeatureStageCriterion => ({
          name: c['name'] as string,
          description: c['description'] as string,
          currentValue: c['current_value'] as number,
          requiredRepr: c['required_repr'] as string,
          met: c['met'] as boolean,
        })),
        failedScreens: (si['failed_screens'] as string[]) ?? [],
      } satisfies FeatureStageInfo,
      finalDecision: r['final_decision'] as string,
    };
  }
}
