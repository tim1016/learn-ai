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

import { ResearchResult } from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { FeatureReportComponent } from '../feature-report/feature-report.component';
import { RunProgressPanelComponent } from '../shared/run-progress-panel/run-progress-panel.component';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { CheckboxModule } from 'primeng/checkbox';
import { TagModule } from 'primeng/tag';

interface FeatureOption {
  label: string;
  value: string;
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
  passed_validation: boolean;
  error: string | null;
}

@Component({
  selector: 'app-feature-runner',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    FeatureReportComponent,
    RunProgressPanelComponent,
    Select,
    InputText,
    ButtonModule,
    MessageModule,
    CheckboxModule,
    TagModule,
  ],
  templateUrl: './feature-runner.component.html',
  styleUrls: ['./feature-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeatureRunnerComponent {
  private jobsService = inject(JobsService);
  private destroyRef = inject(DestroyRef);

  // Form inputs
  ticker = signal('AAPL');
  featureName = signal('momentum_5m');
  fromDate = signal('2024-01-01');
  toDate = signal('2024-03-31');
  timespan = signal('minute');
  multiplier = signal(1);
  forceRun = signal(false);

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

  features: FeatureOption[] = [
    { label: '5-Minute Momentum', value: 'momentum_5m' },
    { label: 'RSI (14)', value: 'rsi_14' },
    { label: 'Realized Volatility (30)', value: 'realized_vol_30' },
    { label: 'Volume Z-Score', value: 'volume_zscore' },
    { label: 'MACD Signal', value: 'macd_signal' },
  ];

  timespanOptions: FeatureOption[] = [
    { label: 'Minute', value: 'minute' },
    { label: 'Hour', value: 'hour' },
    { label: 'Day', value: 'day' },
  ];

  // ── Range guard ─────────────────────────────────────
  // Minute-resolution research at 2 years exceeds ~200k bars, which overruns
  // the backend→python keep-alive window. Cap minute studies to 180 calendar
  // days; hour at 3 years; day unrestricted.
  private readonly MAX_DAYS_BY_TIMESPAN: Readonly<Record<string, number>> = {
    minute: 180,
    hour: 1095,
    day: Number.POSITIVE_INFINITY,
  };

  readonly maxRangeDays = computed<number>(() =>
    this.MAX_DAYS_BY_TIMESPAN[this.timespan()] ?? 180,
  );

  readonly rangeDays = computed<number | null>(() => {
    const from = Date.parse(this.fromDate());
    const to = Date.parse(this.toDate());
    if (Number.isNaN(from) || Number.isNaN(to) || to < from) return null;
    return Math.round((to - from) / 86_400_000);
  });

  readonly rangeWarning = computed<string | null>(() => {
    const days = this.rangeDays();
    if (days === null) return null;
    const cap = this.maxRangeDays();
    if (days <= cap) return null;
    const capLabel = Number.isFinite(cap) ? `${cap} days` : 'no cap';
    return `${this.timespan()}-resolution research is capped at ${capLabel} (you selected ${days} days). Long minute-bar requests don't survive the backend→python connection window. Shorten the range, or switch to an hourly/daily timespan.`;
  });

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.featureName().trim().length > 0 &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
      this.rangeWarning() === null &&
      !this.loading()
    );
  });

  get selectedFeatureLabel(): string {
    const found = this.features.find(f => f.value === this.featureName());
    return found ? found.label : this.featureName();
  }

  get runSummary(): string {
    return `${this.selectedFeatureLabel} on ${this.ticker().toUpperCase()} (${this.fromDate()} to ${this.toDate()})`;
  }

  constructor() {
    let lastLogTs = 0;
    let resultFetchedFor: string | null = null;

    effect(() => {
      this.jobsService.jobs(); // dependency
      const id = this.jobId();
      if (!id) return;
      const j = this.jobsService.job(id);
      if (!j) return;

      // Forward log lines into the rolling buffer.
      for (const log of j.recentLogs) {
        if (log.ts > lastLogTs) {
          const level = pythonLevelToEntryLevel(log.level);
          this.logBuffer.append(level, glyphForLevel(level), log.message);
          lastLogTs = log.ts;
        }
      }

      if (j.status === 'completed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        void this.handleCompleted(id);
      } else if (j.status === 'failed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.errorMessage ?? 'Feature research run failed');
      } else if (j.status === 'cancelled' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.message ?? 'Run cancelled');
      }
    });
  }

  async runResearch(): Promise<void> {
    this.error.set(null);
    this.result.set(null);
    this.logBuffer.clear();

    try {
      const id = await this.jobsService.startJob('feature_research', {
        ticker: this.ticker().toUpperCase(),
        feature_name: this.featureName(),
        from_date: this.fromDate(),
        to_date: this.toDate(),
        timespan: this.timespan(),
        multiplier: this.multiplier(),
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
    } catch {
      // The terminal SSE event makes the cancel moot; the panel will
      // pick up the final state regardless.
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
      quantileBins: (raw.quantile_bins ?? []) as ResearchResult['quantileBins'],
      isMonotonic: raw.is_monotonic,
      monotonicityRatio: raw.monotonicity_ratio,
      passedValidation: raw.passed_validation,
      robustness: raw.robustness as ResearchResult['robustness'],
      featureSpec: raw.feature_spec as ResearchResult['featureSpec'],
      validationVerdict: raw.validation_verdict as ResearchResult['validationVerdict'],
      error: raw.error ?? undefined,
    };
  }
}
