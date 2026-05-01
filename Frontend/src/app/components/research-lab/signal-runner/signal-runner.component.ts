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

import { SignalEngineResult } from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { SignalReportComponent } from '../signal-report/signal-report.component';
import { RunProgressPanelComponent } from '../shared/run-progress-panel/run-progress-panel.component';
import { Select } from 'primeng/select';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { ToggleSwitch } from 'primeng/toggleswitch';
import { TagModule } from 'primeng/tag';

interface FeatureOption {
  label: string;
  value: string;
}

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
  ],
  templateUrl: './signal-runner.component.html',
  styleUrls: ['./signal-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalRunnerComponent {
  private jobsService = inject(JobsService);
  private destroyRef = inject(DestroyRef);

  // Form inputs
  ticker = signal('AAPL');
  featureName = signal('momentum_5m');
  fromDate = signal('2024-01-01');
  toDate = signal('2024-06-30');
  flipSign = signal(true);
  regimeGateEnabled = signal(true);
  forceRefresh = signal(false);

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

  features: FeatureOption[] = [
    { label: '5-Minute Momentum', value: 'momentum_5m' },
    { label: 'RSI (14)', value: 'rsi_14' },
    { label: 'Realized Volatility (30)', value: 'realized_vol_30' },
    { label: 'Volume Z-Score', value: 'volume_zscore' },
    { label: 'MACD Signal', value: 'macd_signal' },
  ];

  canRun = computed(() => {
    return (
      this.ticker().trim().length > 0 &&
      this.featureName().trim().length > 0 &&
      this.fromDate().trim().length > 0 &&
      this.toDate().trim().length > 0 &&
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
      const id = await this.jobsService.startJob('signal_engine', {
        ticker: this.ticker().toUpperCase(),
        feature_name: this.featureName(),
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

  /** Transform snake_case worker payload → camelCase SignalEngineResult.
   *  The nested dataclasses keep their snake_case keys; the report
   *  component already tolerates both forms (the legacy GraphQL path
   *  delivers camelCase, this one delivers snake_case nested). */
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
      backtestGrid: (raw.backtest_grid ?? []) as SignalEngineResult['backtestGrid'],
      walkForward: raw.walk_forward as SignalEngineResult['walkForward'],
      graduation: raw.graduation as SignalEngineResult['graduation'],
      signalDiagnostics: raw.signal_diagnostics as SignalEngineResult['signalDiagnostics'],
      dataSufficiency: raw.data_sufficiency as SignalEngineResult['dataSufficiency'],
      effectiveSample: raw.effective_sample as SignalEngineResult['effectiveSample'],
      regimeCoverage: regimeCoverageEntries,
      jointRegimeCoverage: (raw.joint_regime_coverage ?? []) as SignalEngineResult['jointRegimeCoverage'],
      signalBehavior: raw.signal_behavior as SignalEngineResult['signalBehavior'],
      oosSharpeCi: raw.oos_sharpe_ci as SignalEngineResult['oosSharpeCi'],
      deflatedSharpe: raw.deflated_sharpe as SignalEngineResult['deflatedSharpe'],
      methodology: raw.methodology as SignalEngineResult['methodology'],
      researchLog: raw.research_log ?? '',
      error: raw.error ?? undefined,
    };
  }
}
