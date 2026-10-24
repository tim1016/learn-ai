import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';

import { ButtonModule } from 'primeng/button';
import { CardModule } from 'primeng/card';
import { CheckboxModule } from 'primeng/checkbox';
import { MessageModule } from 'primeng/message';
import { ProgressBarModule } from 'primeng/progressbar';
import { SelectModule } from 'primeng/select';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { TooltipModule } from 'primeng/tooltip';

import {
  AggregateIcCi,
  BatchResearchResult,
  BinomialNullTest,
  CrossSectionalCriterion,
  CrossSectionalDataSource,
  CrossSectionalFailure,
  CrossSectionalStageInfo,
  MemberExclusion,
  ProviderInputSource,
  StudyUniverse,
  TickerBatchResult,
  TickerValidity,
  ValiditySummary,
} from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { RunProgressPanelComponent } from '../shared/run-progress-panel/run-progress-panel.component';
import { MultiTickerRangePickerComponent } from '../../../shared/multi-ticker-range-picker/multi-ticker-range-picker.component';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { DataSourceComponent } from '../../../shared/data-source/data-source.component';
import { LakeStudySourceComponent } from '../../../shared/lake-study/lake-study-source.component';
import {
  mapLakeStudyDataSource,
  readLakeStudyRefusal,
  type LakeDataSourceRaw,
} from '../../../shared/lake-study/lake-study';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { MultiTickerRange } from '../../../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';
import { multiTickerRangeToWire } from '../../../utils/ticker-wire';

const DEFAULT_TICKERS = ['SPY', 'QQQ', 'AAPL'];

const OPTIONS_FEATURES = [
  { label: 'IV 30-Day ATM', value: 'iv_30d' },
  { label: 'IV Rank (60-Day)', value: 'iv_rank_60' },
  { label: 'Log Put-Call Skew', value: 'log_skew' },
  { label: 'IV Rank (252-Day)', value: 'iv_rank_252' },
  { label: 'VRP (5-Day)', value: 'vrp_5' },
];

const TARGET_TYPES = [
  { label: 'Directional (1d forward return)', value: 'directional' },
  { label: 'Volatility (5d forward RV)', value: 'volatility' },
  { label: 'Absolute Return', value: 'abs_return' },
];

interface ProviderInputSourceRaw {
  provider: string;
  endpoint?: string;
  attempted?: number;
  failed?: number;
  observation_digest?: string | null;
  reproducible: boolean;
}

/** The ``data_source`` block (``cross_sectional_study_service.data_source_block``). */
interface CrossSectionalDataSourceRaw {
  kind: 'composite';
  calculation_version: string;
  /** The block every lake study writes; a universe's counts are member-sessions. */
  stock_bars: LakeDataSourceRaw | null;
  option_inputs: {
    inputs: Record<'option_expiries' | 'option_contracts' | 'option_bars', ProviderInputSourceRaw>;
    risk_free_rate: {
      provider: string;
      days_from_fred: number;
      days_from_default: number;
      days_fetch_failed: number;
      reproducible: boolean;
    };
    failures_total: number;
  } | null;
}

/** What a study that ended without a result parks as the job's result. */
interface CrossSectionalFailedResultRaw {
  success: false;
  status: 'refused' | 'not_evaluated';
  universe?: StudyUniverseRaw;
  data_source?: CrossSectionalDataSourceRaw;
}

interface StudyUniverseRaw {
  requested: string[];
  requested_count: number;
  evaluated: string[];
  evaluated_count: number;
  excluded: MemberExclusion[];
  excluded_count: number;
  degraded: { ticker: string; failures_total: number; rate_days_fetch_failed: number }[];
  denominator: number;
  inclusion_policy: { name: string; description: string };
  complete: boolean;
}

/** Snake-case shape returned by the cross-sectional jobs-internal worker. */
interface CrossSectionalJobResultRaw {
  success: boolean;
  status?: 'complete' | 'partial';
  universe?: StudyUniverseRaw;
  data_source?: CrossSectionalDataSourceRaw;
  feature_name: string;
  target_type?: string;
  tickers_tested: number;
  tickers_tested_raw?: number;
  tickers_valid?: number;
  tickers_passed: number;
  pass_rate: number;
  cross_sectional_consistent: boolean;
  aggregate_ic: number;
  aggregate_ic_uniform?: number;
  aggregate_ic_ci?: {
    point: number;
    se: number;
    ci_lower: number;
    ci_upper: number;
    confidence_level: number;
    weighting_method: string;
    se_approximation_note?: string;
    n_tickers_used: number;
    sum_weights: number;
    valid: boolean;
  };
  binomial_test?: {
    n_valid: number;
    n_eff_assets: number;
    n_passed: number;
    alpha_per_ticker: number;
    p_value: number;
    significant: boolean;
  };
  n_eff_assets?: number;
  n_eff_assets_method?: 'ic' | 'returns';
  validity_summary?: {
    valid: number;
    invalid_iv: number;
    invalid_data: number;
    errored: number;
  };
  stage_info?: {
    stage: number;
    label: string;
    description: string;
    next_stage_label: string;
    failed_criteria: {
      name: string;
      description: string;
      current_value: number;
      required_repr: string;
      met: boolean;
    }[];
    advance_criteria: {
      name: string;
      description: string;
      current_value: number;
      required_repr: string;
      met: boolean;
    }[];
  };
  ticker_results: {
    ticker: string;
    mean_ic: number;
    ic_t_stat: number;
    ic_p_value: number;
    nw_t_stat: number;
    nw_p_value: number;
    effective_n: number;
    is_stationary: boolean;
    passed_validation: boolean;
    data_points: number;
    error: string | null;
    validity?: TickerValidity;
    low_confidence?: boolean;
    exclusion?: MemberExclusion | null;
    option_inputs?: { failures_total: number } | null;
  }[];
  summary: string;
}

/** One line for the option inputs: the provider's live answers, not archived, and what went unanswered. */
function optionInputsText(source: CrossSectionalDataSource | null): string {
  const options = source?.optionInputs;
  if (!options) return '';
  const provider = Object.values(options.inputs)[0]?.provider ?? 'provider';
  const rate = options.riskFreeRate;
  const outage = rate.daysFetchFailed > 0 ? ` (${rate.daysFetchFailed} because FRED did not answer)` : '';
  return (
    `Option inputs: ${provider} live requests, not archived — ${options.failuresTotal} failed; ` +
    `risk-free rate: ${rate.provider.toUpperCase()}, ${rate.daysFromDefault} days on the default${outage}`
  );
}

@Component({
  selector: 'app-batch-runner',
  imports: [
    CommonModule,
    FormsModule,
    ButtonModule,
    SelectModule,
    TableModule,
    TagModule,
    TooltipModule,
    ProgressBarModule,
    MessageModule,
    CheckboxModule,
    CardModule,
    RunProgressPanelComponent,
    MultiTickerRangePickerComponent,
    AssetIdentityComponent,
    DataSourceComponent,
    LakeStudySourceComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './batch-runner.component.html',
  styleUrl: './batch-runner.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BatchRunnerComponent {
  private jobsService = inject(JobsService);

  // Form state
  featureName = signal('iv_rank_60');
  targetType = signal('directional');

  // Single MultiTickerRange replaces (selectedTickers, fromDate, toDate).
  // The study runs on daily bars only (its targets look one and five sessions
  // ahead), so the picker's Sampling card is hidden and the range is fixed at
  // daily / multiplier 1; the backend refuses any other resolution with a reason.
  range = signal<MultiTickerRange>({
    symbols: [...DEFAULT_TICKERS],
    from: '2025-09-02',
    to: '2025-12-01',
    resolution: 'daily',
    multiplier: 1,
  });

  // Run state — survives tab switches because JobsService is providedIn:'root'.
  // We track the active job id locally; the JobsService holds the canonical
  // per-job state and event log.
  readonly jobId = signal<string | null>(null);
  readonly result = signal<BatchResearchResult | null>(null);
  readonly error = signal<string | null>(null);
  /** Why a run that failed has no result, read from the payload the failed job parked. */
  readonly failure = signal<CrossSectionalFailure | null>(null);

  /** Rolling FIFO log buffer (cap 500). Replaces the unbounded
   *  cumulativeLogs signal that used to live inline. */
  readonly logBuffer = new RunLogBuffer();
  readonly logEntries = this.logBuffer.entries;

  // Surface the current JobState as a computed for the template.
  readonly job = computed<JobState | null>(() => {
    const id = this.jobId();
    if (!id) return null;
    return this.jobsService.job(id) ?? null;
  });

  readonly loading = computed<boolean>(() => {
    const j = this.job();
    return j !== null && (j.status === 'queued' || j.status === 'running');
  });

  readonly features = OPTIONS_FEATURES;
  readonly targetTypes = TARGET_TYPES;

  constructor() {
    // Watch the JobsService for changes to the active job. We use an
    // effect that depends on `jobsService.jobs()` so any event delivery
    // (which updates that signal) re-fires the effect and we can:
    //   1. accumulate log lines into our running buffer (the service
    //      caps `recentLogs` to the last 5),
    //   2. detect terminal status and fetch the result blob once.
    let lastLogSeq = -1;
    let resultFetchedFor: string | null = null;

    effect(() => {
      // touch the jobs() signal so the effect fires on each event
      this.jobsService.jobs();
      const id = this.jobId();
      if (!id) return;
      const j = this.jobsService.job(id);
      if (!j) return;

      // Accumulate any new log lines into the shared RunLogBuffer
      // (capped at 500 entries; older lines drop off silently).
      // JobsService.recentLogs is sliced to the last 5; this buffer keeps
      // the rolling history for the panel. Dedupe by the monotonic
      // ``seq`` rather than ``ts`` because two SSE log events landing in
      // the same wall-clock millisecond would otherwise collapse into
      // one and the dropped line never reappears once it slides past the
      // 5-entry window.
      for (const log of j.recentLogs) {
        if (log.seq > lastLogSeq) {
          const level = pythonLevelToEntryLevel(log.level);
          this.logBuffer.append(level, glyphForLevel(level), log.message);
          lastLogSeq = log.seq;
        }
      }

      if (j.status === 'failed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.errorMessage ?? 'Cross-sectional run failed');
        void this.loadFailure(id);
      } else if (j.status === 'cancelled' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.message ?? 'Run cancelled');
      } else if (j.status === 'completed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        void this.handleCompleted(id);
      }
    });
  }

  get passRatePct(): number {
    return (this.result()?.passRate ?? 0) * 100;
  }

  get consistentSeverity(): 'success' | 'danger' {
    return this.result()?.crossSectionalConsistent ? 'success' : 'danger';
  }

  get consistentLabel(): string {
    return this.result()?.crossSectionalConsistent ? 'Consistent' : 'Not Consistent';
  }

  // ─── Verdict helpers (new schema only) ───────────────────────────

  /** Stage 0/1/2/3 from the SSE-driven response. ``null`` when the
   *  response doesn't populate ``stageInfo``. */
  readonly stage = computed<0 | 1 | 2 | 3 | null>(
    () => this.result()?.stageInfo?.stage ?? null,
  );

  readonly stageBand = computed<'green' | 'amber' | 'red' | 'na'>(() => {
    const s = this.stage();
    if (s === null) return 'na';
    if (s === 3) return 'green';
    if (s === 2) return 'green';
    if (s === 1) return 'amber';
    return 'red';
  });

  readonly aggregateIcCi = computed<AggregateIcCi | null>(
    () => this.result()?.aggregateIcCi ?? null,
  );

  readonly binomialTest = computed<BinomialNullTest | null>(
    () => this.result()?.binomialTest ?? null,
  );

  readonly validitySummary = computed<ValiditySummary | null>(
    () => this.result()?.validitySummary ?? null,
  );

  readonly stageInfo = computed<CrossSectionalStageInfo | null>(
    () => this.result()?.stageInfo ?? null,
  );

  readonly ciStraddlesZero = computed<boolean>(() => {
    const ci = this.aggregateIcCi();
    return !!ci?.valid && ci.ciLower < 0 && ci.ciUpper > 0;
  });

  /** "0.0281 ± 0.0156" form for the headline; falls back to "0.0281"
   *  when the CI was not computed (legacy path). */
  readonly aggregateIcDisplay = computed<string>(() => {
    const r = this.result();
    if (!r) return '—';
    const ci = this.aggregateIcCi();
    if (ci?.valid) {
      const halfWidth = (ci.ciUpper - ci.ciLower) / 2;
      return `${ci.point.toFixed(4)} ± ${halfWidth.toFixed(4)}`;
    }
    return r.aggregateIc.toFixed(4);
  });

  readonly ciIntervalDisplay = computed<string | null>(() => {
    const ci = this.aggregateIcCi();
    if (!ci?.valid) return null;
    return `95% CI [${ci.ciLower.toFixed(4)}, ${ci.ciUpper.toFixed(4)}]`;
  });

  /** Per-ticker UI tag — distinguishes PASS / FAIL / INVALID / ERROR. */
  validityTagSeverity(row: TickerBatchResult): 'success' | 'danger' | 'warn' | 'info' {
    const v: TickerValidity = row.validity ?? 'valid';
    if (v === 'invalid_iv' || v === 'invalid_data') return 'warn';
    if (v === 'error') return 'info';
    return row.passedValidation ? 'success' : 'danger';
  }

  validityTagLabel(row: TickerBatchResult): string {
    const v: TickerValidity = row.validity ?? 'valid';
    if (v === 'invalid_iv') return 'INVALID — IV';
    if (v === 'invalid_data') return 'INVALID — DATA';
    if (v === 'error') return 'ERROR';
    if (row.lowConfidence) {
      return row.passedValidation ? 'PASS · low conf' : 'FAIL · low conf';
    }
    return row.passedValidation ? 'PASS' : 'FAIL';
  }

  /** True when the CI half-width exceeds |IC| by a wide margin —
   *  i.e. the band is much larger than the point estimate, so the CI
   *  is technically computable but not interpretable. Used to render
   *  a banner under the headline. */
  readonly ciUnreliable = computed<boolean>(() => {
    const ci = this.aggregateIcCi();
    if (!ci?.valid) return false;
    const halfWidth = Math.abs(ci.ciUpper - ci.ciLower) / 2;
    const magnitude = Math.abs(ci.point);
    // Heuristic: half-width > 5× |IC| means the CI is essentially
    // uninformative even though the math went through.
    return magnitude > 0 && halfWidth > 5 * magnitude;
  });

  /** Disclosure label for which input matrix drove `nEffAssets`. */
  readonly nEffAssetsMethodLabel = computed<string>(() => {
    const m = this.result()?.nEffAssetsMethod;
    if (m === 'ic') return 'IC time series';
    if (m === 'returns') return 'daily stock returns (Stage-1 fallback)';
    return 'unknown';
  });

  /** The members left out of the statistics, each with its reason (T18). */
  readonly universe = computed<StudyUniverse | null>(() => this.result()?.universe ?? null);

  readonly isPartial = computed<boolean>(() => this.result()?.status === 'partial');

  readonly dataSource = computed<CrossSectionalDataSource | null>(() => this.result()?.dataSource ?? null);

  /** True when the result on screen is a stored one rather than a fresh computation. */
  readonly servedFromCache = computed<boolean>(() => this.dataSource()?.stockBars?.cache.hit ?? false);

  readonly cachedAtMs = computed<number | null>(() => this.dataSource()?.stockBars?.cache.cachedAtMs ?? null);

  /** The lake's refusal when the stock bars could not be admitted: its reason, counts and each session that failed. */
  readonly refusal = computed(() => this.failure()?.refused ?? null);

  /**
   * One line for the option inputs, which are not the lake's: the provider's live answers, not archived. The stock
   * bars have the shared lake data-source line; the result as a whole is not labelled "lake".
   */
  readonly optionInputsLine = computed<string>(() => optionInputsText(this.dataSource()));

  /** The same line for a run that ended without a result, when it got as far as asking the provider. */
  readonly failureOptionInputsLine = computed<string>(() => optionInputsText(this.failure()?.dataSource ?? null));

  /** What left a member short of what was asked, in words. */
  degradedReason(d: { failuresTotal: number; rateDaysFetchFailed: number }): string {
    const parts: string[] = [];
    if (d.failuresTotal > 0) parts.push(`${d.failuresTotal} option request(s) went unanswered while its IV history was built`);
    if (d.rateDaysFetchFailed > 0) {
      parts.push(`${d.rateDaysFetchFailed} day(s) were priced at the default risk-free rate because FRED did not answer`);
    }
    return parts.join(' and ');
  }

  partialText(uni: StudyUniverse): string {
    return (
      `${uni.evaluatedCount} of ${uni.requestedCount} requested members are in the statistics, so every statistic ` +
      `divides by ${uni.denominator}. ${uni.inclusionPolicy.description}`
    );
  }

  unevaluatedText(uni: StudyUniverse): string {
    return `${uni.evaluatedCount} of ${uni.requestedCount} requested members are in the statistics. ${uni.inclusionPolicy.description}`;
  }

  // Note: ticker selection (toggle / All / None) now lives inside
  // <app-multi-ticker-range-picker>. The component refuses to leave the
  // symbols array empty (keeps at least one selected); the explicit
  // empty-list guard below catches any edge case where consumers reach
  // runBatch with an empty universe.

  /** Start the study. ``force`` skips the stored result for these exact inputs and recomputes. */
  async runBatch(force = false): Promise<void> {
    if (this.range().symbols.length === 0) {
      this.error.set('Select at least one ticker');
      return;
    }

    this.error.set(null);
    this.failure.set(null);
    this.result.set(null);
    this.logBuffer.clear();

    try {
      // Wire the canonical MultiTickerRange shape via the shared adapter.
      // CrossSectionalJobRequest inherits MultiTickerRequest in PR (ii);
      // legacy 'tickers' alias still accepted until PR (iii)'s alias
      // removal commit, but we send canonical 'symbols' now.
      const wire = multiTickerRangeToWire({
        ...this.range(),
        symbols: this.range().symbols.map((s) => s.toUpperCase()),
      });
      const id = await this.jobsService.startJob('cross_sectional', {
        ...wire,
        feature_name: this.featureName(),
        target_type: this.targetType(),
        force,
      });
      this.jobId.set(id);
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to start cross-sectional run';
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
      // doesn't leave the UI silently stuck on "running".
      const status = this.jobsService.job(id)?.status;
      if (status === 'completed' || status === 'cancelled' || status === 'failed') {
        return;
      }
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to cancel cross-sectional run';
      this.error.set(msg);
    }
  }

  newRun(): void {
    const id = this.jobId();
    if (id) {
      this.jobsService.dismiss(id);
    }
    this.jobId.set(null);
    this.result.set(null);
    this.error.set(null);
    this.failure.set(null);
    this.logBuffer.clear();
  }

  getIcClass(ic: number): string {
    const abs = Math.abs(ic);
    if (abs >= 0.05) return 'text-green-500 font-bold';
    if (abs >= 0.03) return 'text-yellow-500';
    return 'text-red-500';
  }

  async onPanelCancel(): Promise<void> {
    await this.cancelRun();
  }

  private async handleCompleted(id: string): Promise<void> {
    try {
      const raw = await this.jobsService.fetchResult<CrossSectionalJobResultRaw>(id);
      this.result.set(this.toBatchResearchResult(raw));
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to fetch cross-sectional result';
      this.error.set(msg);
    }
  }

  /**
   * A run that ended without a result parks why as the job's result: the lake's refusal, or the universe when no
   * member could be evaluated. A job that failed for any other reason parks nothing, and the fetch finds none: the
   * failure message already shown is all there is.
   */
  private async loadFailure(id: string): Promise<void> {
    try {
      const raw = await this.jobsService.fetchResult<CrossSectionalFailedResultRaw>(id);
      if (this.jobId() !== id) return;
      this.failure.set({
        // A refusal's source is the lake part of the composite block: read it with the shared reader.
        refused: readLakeStudyRefusal({ ...raw, data_source: raw.data_source?.stock_bars ?? null }),
        universe: raw.universe ? this.toUniverse(raw.universe) : null,
        dataSource: raw.data_source ? this.toDataSource(raw.data_source) : null,
      });
    } catch {
      // No parked result: the job failed on something other than the stock bars or an empty universe.
    }
  }

  /** Transform the snake_case worker result into the camelCase
   *  ``BatchResearchResult`` interface used by the table template. */
  private toBatchResearchResult(raw: CrossSectionalJobResultRaw): BatchResearchResult {
    return {
      success: raw.success,
      featureName: raw.feature_name,
      tickersTested: raw.tickers_tested,
      tickersPassed: raw.tickers_passed,
      passRate: raw.pass_rate,
      crossSectionalConsistent: raw.cross_sectional_consistent,
      aggregateIc: raw.aggregate_ic,
      tickerResults: raw.ticker_results.map((r) => ({
        ticker: r.ticker,
        meanIc: r.mean_ic,
        icTStat: r.ic_t_stat,
        icPValue: r.ic_p_value,
        nwTStat: r.nw_t_stat,
        nwPValue: r.nw_p_value,
        effectiveN: r.effective_n,
        isStationary: r.is_stationary,
        passedValidation: r.passed_validation,
        dataPoints: r.data_points,
        error: r.error ?? undefined,
        validity: r.validity,
        lowConfidence: r.low_confidence,
        exclusion: r.exclusion ?? undefined,
        optionRequestsFailed: r.option_inputs?.failures_total || undefined,
      })),
      summary: raw.summary,
      status: raw.status,
      universe: raw.universe ? this.toUniverse(raw.universe) : undefined,
      dataSource: raw.data_source ? this.toDataSource(raw.data_source) : undefined,
      tickersTestedRaw: raw.tickers_tested_raw,
      tickersValid: raw.tickers_valid,
      validitySummary: raw.validity_summary
        ? {
            valid: raw.validity_summary.valid,
            invalidIv: raw.validity_summary.invalid_iv,
            invalidData: raw.validity_summary.invalid_data,
            errored: raw.validity_summary.errored,
          }
        : undefined,
      aggregateIcUniform: raw.aggregate_ic_uniform,
      aggregateIcCi: raw.aggregate_ic_ci
        ? {
            point: raw.aggregate_ic_ci.point,
            se: raw.aggregate_ic_ci.se,
            ciLower: raw.aggregate_ic_ci.ci_lower,
            ciUpper: raw.aggregate_ic_ci.ci_upper,
            confidenceLevel: raw.aggregate_ic_ci.confidence_level,
            weightingMethod: raw.aggregate_ic_ci.weighting_method,
            seApproximationNote: raw.aggregate_ic_ci.se_approximation_note,
            nTickersUsed: raw.aggregate_ic_ci.n_tickers_used,
            sumWeights: raw.aggregate_ic_ci.sum_weights,
            valid: raw.aggregate_ic_ci.valid,
          }
        : undefined,
      binomialTest: raw.binomial_test
        ? {
            nValid: raw.binomial_test.n_valid,
            nEffAssets: raw.binomial_test.n_eff_assets,
            nPassed: raw.binomial_test.n_passed,
            alphaPerTicker: raw.binomial_test.alpha_per_ticker,
            pValue: raw.binomial_test.p_value,
            significant: raw.binomial_test.significant,
          }
        : undefined,
      nEffAssets: raw.n_eff_assets,
      nEffAssetsMethod: raw.n_eff_assets_method,
      stageInfo: raw.stage_info
        ? {
            stage: raw.stage_info.stage as 0 | 1 | 2 | 3,
            label: raw.stage_info.label,
            description: raw.stage_info.description,
            nextStageLabel: raw.stage_info.next_stage_label,
            failedCriteria: raw.stage_info.failed_criteria.map(this.toCriterion),
            advanceCriteria: raw.stage_info.advance_criteria.map(this.toCriterion),
          }
        : undefined,
    };
  }

  private toUniverse(u: StudyUniverseRaw): StudyUniverse {
    return {
      requested: u.requested,
      requestedCount: u.requested_count,
      evaluated: u.evaluated,
      evaluatedCount: u.evaluated_count,
      excluded: u.excluded,
      excludedCount: u.excluded_count,
      degraded: u.degraded.map((d) => ({
        ticker: d.ticker,
        failuresTotal: d.failures_total,
        rateDaysFetchFailed: d.rate_days_fetch_failed,
      })),
      denominator: u.denominator,
      inclusionPolicy: u.inclusion_policy,
      complete: u.complete,
    };
  }

  private toDataSource(raw: CrossSectionalDataSourceRaw): CrossSectionalDataSource {
    const provider = (p: ProviderInputSourceRaw): ProviderInputSource => ({
      provider: p.provider,
      endpoint: p.endpoint,
      attempted: p.attempted,
      failed: p.failed,
      observationDigest: p.observation_digest,
      reproducible: p.reproducible,
    });
    const options = raw.option_inputs;
    return {
      kind: raw.kind,
      calculationVersion: raw.calculation_version,
      stockBars: raw.stock_bars ? mapLakeStudyDataSource(raw.stock_bars) : null,
      optionInputs: options
        ? {
            inputs: {
              option_expiries: provider(options.inputs.option_expiries),
              option_contracts: provider(options.inputs.option_contracts),
              option_bars: provider(options.inputs.option_bars),
            },
            riskFreeRate: {
              provider: options.risk_free_rate.provider,
              daysFromFred: options.risk_free_rate.days_from_fred,
              daysFromDefault: options.risk_free_rate.days_from_default,
              daysFetchFailed: options.risk_free_rate.days_fetch_failed,
              reproducible: options.risk_free_rate.reproducible,
            },
            failuresTotal: options.failures_total,
          }
        : null,
    };
  }

  private toCriterion(c: {
    name: string;
    description: string;
    current_value: number;
    required_repr: string;
    met: boolean;
  }): CrossSectionalCriterion {
    return {
      name: c.name,
      description: c.description,
      currentValue: c.current_value,
      requiredRepr: c.required_repr,
      met: c.met,
    };
  }
}
