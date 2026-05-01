import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';

import { ButtonModule } from 'primeng/button';
import { CardModule } from 'primeng/card';
import { CheckboxModule } from 'primeng/checkbox';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { ProgressBarModule } from 'primeng/progressbar';
import { SelectModule } from 'primeng/select';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import {
  AggregateIcCi,
  BatchResearchResult,
  BinomialNullTest,
  CrossSectionalCriterion,
  CrossSectionalStageInfo,
  TickerBatchResult,
  TickerValidity,
  ValiditySummary,
} from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';

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

/** Snake-case shape returned by the cross-sectional jobs-internal worker. */
interface CrossSectionalJobResultRaw {
  success: boolean;
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
  }[];
  summary: string;
}

interface LogLine {
  level: string;
  message: string;
  ts: number;
}

@Component({
  selector: 'app-batch-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ButtonModule,
    SelectModule,
    InputTextModule,
    TableModule,
    TagModule,
    ProgressBarModule,
    MessageModule,
    CheckboxModule,
    CardModule,
  ],
  templateUrl: './batch-runner.component.html',
  styleUrl: './batch-runner.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BatchRunnerComponent {
  private jobsService = inject(JobsService);
  private destroyRef = inject(DestroyRef);

  // Form state
  featureName = signal('iv_rank_60');
  fromDate = signal('2025-09-01');
  toDate = signal('2025-12-01');
  targetType = signal('directional');
  selectedTickers = signal<string[]>([...DEFAULT_TICKERS]);

  // Run state — survives tab switches because JobsService is providedIn:'root'.
  // We track the active job id locally; the JobsService holds the canonical
  // per-job state and event log.
  readonly jobId = signal<string | null>(null);
  readonly result = signal<BatchResearchResult | null>(null);
  readonly error = signal<string | null>(null);
  readonly cumulativeLogs = signal<LogLine[]>([]);

  // Surface the current JobState as a computed for the template.
  readonly job = computed<JobState | null>(() => {
    const id = this.jobId();
    if (!id) return null;
    return this.jobsService.job(id) ?? null;
  });

  // Tick the JobsService snapshot every 250ms so log streaming feels live
  // without us subscribing to its private update channel. JobsService
  // updates its internal map on each event; we just re-read it.
  private readonly jobsTick = signal(0);

  readonly loading = computed<boolean>(() => {
    const j = this.job();
    return j !== null && (j.status === 'queued' || j.status === 'running');
  });

  readonly progressPercent = computed<number>(() => {
    const j = this.job();
    if (!j || !j.total) return 0;
    return Math.min(100, Math.round((j.current ?? 0) / j.total * 100));
  });

  readonly progressMessage = computed<string>(() => {
    const j = this.job();
    if (!j) return '';
    if (j.message) return j.message;
    if (j.phase) return `Phase: ${j.phase.replaceAll('_', ' ')}`;
    return j.status;
  });

  readonly features = OPTIONS_FEATURES;
  readonly targetTypes = TARGET_TYPES;
  readonly allTickers = DEFAULT_TICKERS;

  constructor() {
    // Watch the JobsService for changes to the active job. We use an
    // effect that depends on `jobsService.jobs()` so any event delivery
    // (which updates that signal) re-fires the effect and we can:
    //   1. accumulate log lines into our running buffer (the service
    //      caps `recentLogs` to the last 5),
    //   2. detect terminal status and fetch the result blob once.
    let lastLogTs = 0;
    let resultFetchedFor: string | null = null;

    effect(() => {
      // touch the jobs() signal so the effect fires on each event
      this.jobsService.jobs();
      const id = this.jobId();
      if (!id) return;
      const j = this.jobsService.job(id);
      if (!j) return;

      // Accumulate any new log lines. recentLogs is sliced to the most
      // recent 5 by JobsService; we keep the full history here for the
      // event-log panel.
      for (const log of j.recentLogs) {
        if (log.ts > lastLogTs) {
          this.cumulativeLogs.update((arr) => [...arr, log]);
          lastLogTs = log.ts;
        }
      }

      if (j.status === 'failed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.errorMessage ?? 'Cross-sectional run failed');
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

  /** Stage 0/1/2/3 from the new SSE-driven response. ``null`` for the
   *  legacy GraphQL path that doesn't populate ``stageInfo``. */
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
    return row.passedValidation ? 'PASS' : 'FAIL';
  }

  toggleTicker(ticker: string): void {
    const current = this.selectedTickers();
    if (current.includes(ticker)) {
      this.selectedTickers.set(current.filter((t) => t !== ticker));
    } else {
      this.selectedTickers.set([...current, ticker]);
    }
  }

  selectAll(): void {
    this.selectedTickers.set([...DEFAULT_TICKERS]);
  }

  deselectAll(): void {
    this.selectedTickers.set([]);
  }

  async runBatch(): Promise<void> {
    if (this.selectedTickers().length === 0) {
      this.error.set('Select at least one ticker');
      return;
    }

    this.error.set(null);
    this.result.set(null);
    this.cumulativeLogs.set([]);

    try {
      const id = await this.jobsService.startJob('cross_sectional', {
        feature_name: this.featureName(),
        tickers: this.selectedTickers(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        target_type: this.targetType(),
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
    } catch (err) {
      // Swallow — the worker may have just finished. The terminal event
      // will land via SSE shortly.
      void err;
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
    this.cumulativeLogs.set([]);
  }

  getValidationSeverity(passed: boolean): 'success' | 'danger' {
    return passed ? 'success' : 'danger';
  }

  getIcClass(ic: number): string {
    const abs = Math.abs(ic);
    if (abs >= 0.05) return 'text-green-500 font-bold';
    if (abs >= 0.03) return 'text-yellow-500';
    return 'text-red-500';
  }

  /** Map a log level to the colour band used in the inline event log. */
  logLevelClass(level: string): string {
    if (level === 'error') return 'log-error';
    if (level === 'warn' || level === 'warning') return 'log-warn';
    return 'log-info';
  }

  formatLogTime(ts: number): string {
    const d = new Date(ts);
    return d.toLocaleTimeString('en-US', { hour12: false });
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
      })),
      summary: raw.summary,
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
