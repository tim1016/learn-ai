import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';

import { MessageModule } from 'primeng/message';
import { SelectModule } from 'primeng/select';

import { BatchResearchResult } from '../../../services/research.service';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { RunProgressPanelComponent } from '../shared/run-progress-panel/run-progress-panel.component';
import { MultiTickerRangePickerComponent } from '../../../shared/multi-ticker-range-picker/multi-ticker-range-picker.component';
import type { MultiTickerRange } from '../../../shared/multi-ticker-range-picker/multi-ticker-range-picker.types';
import { multiTickerRangeToWire } from '../../../utils/ticker-wire';
import { StudyPageComponent } from '../../../research/study-page/study-page.component';
import { createRunGate } from '../../../research/run-gate';
import { createRerunPrefill } from '../../../research/stored-study/rerun';
import { StoredInputsError, type StoredInputs } from '../../../research/stored-study/stored-inputs';
import { createResultInputTracker } from '../../../shared/workspace/previous-result';
import { CrossSectionalReportComponent } from './cross-sectional-report/cross-sectional-report.component';
import { CrossSectionalJobResultRaw, toBatchResearchResult } from './cross-sectional-result.mapper';

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

/** What a cross-sectional request is made from: the inputs a shown result answers. */
interface BatchStudyInputs {
  readonly range: MultiTickerRange;
  readonly feature: string;
  readonly target: string;
}

@Component({
  selector: 'app-batch-runner',
  imports: [
    FormsModule,
    SelectModule,
    MessageModule,
    RunProgressPanelComponent,
    MultiTickerRangePickerComponent,
    StudyPageComponent,
    CrossSectionalReportComponent,
  ],
  templateUrl: './batch-runner.component.html',
  styleUrl: './batch-runner.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BatchRunnerComponent {
  private jobsService = inject(JobsService);

  /** `?rerun=<saved run id>`: fill the inputs from that run. It runs nothing; Run study is still the reader's. */
  readonly rerun = input<string>();
  private readonly prefill = createRerunPrefill({
    study: 'cross-sectional-study',
    id: this.rerun,
    apply: (stored) => this.fillFrom(stored),
  });
  readonly prefillNotice = this.prefill.notice;

  // Form state
  featureName = signal('iv_rank_60');
  targetType = signal('directional');

  // Single MultiTickerRange replaces (selectedTickers, fromDate, toDate).
  // Cross-sectional research reads daily bars whatever is sent: the worker
  // ignores the resolution, so the page fixes it at daily / multiplier=1 and
  // offers no sampling control (#3527).
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

  /** One press starts one run: a second click before the job is accepted is dropped. */
  private readonly gate = createRunGate();
  readonly starting = this.gate.starting;

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<BatchStudyInputs>(() => ({
    range: this.range(),
    feature: this.featureName(),
    target: this.targetType(),
  }));
  readonly inputs = createResultInputTracker(this.enteredInputs, ({ range, feature, target }) => {
    const universe = range.symbols.map((symbol) => symbol.toUpperCase()).sort();
    return {
      key: JSON.stringify([universe, range.from, range.to, feature, target]),
      facts: [
        { label: 'Universe', value: universe.join(', ') },
        { label: 'Window', value: `${range.from} to ${range.to}` },
        { label: 'Feature', value: OPTIONS_FEATURES.find((f) => f.value === feature)?.label ?? feature },
        { label: 'Target', value: TARGET_TYPES.find((t) => t.value === target)?.label ?? target },
      ],
    };
  });
  private submitted: BatchStudyInputs | null = null;

  readonly canRun = computed(() => {
    const r = this.range();
    return r.symbols.length > 0 && r.from !== '' && r.to !== '' && !this.loading() && !this.starting();
  });

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => {
    const r = this.range();
    const feature = OPTIONS_FEATURES.find((f) => f.value === this.featureName())?.label ?? this.featureName();
    return `${feature} across ${r.symbols.length} ${r.symbols.length === 1 ? 'instrument' : 'instruments'}, ${r.from} to ${r.to}`;
  });

  /** Why Run study cannot start, in words that point at the step to fix. */
  readonly blockedReason = computed<string | null>(() => {
    if (this.canRun() || this.loading() || this.starting()) return null;
    return 'Choose at least one instrument and both dates.';
  });

  /** Anything past the idle state is on screen: a job, an error or a result. */
  readonly hasOutput = computed(
    () => this.job() !== null || this.error() !== null || this.result() !== null,
  );

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
      } else if (j.status === 'cancelled' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.message ?? 'Run cancelled');
      } else if (j.status === 'completed' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        void this.handleCompleted(id);
      }
    });
  }

  // Note: ticker selection (toggle / All / None) now lives inside
  // <app-multi-ticker-range-picker>. The component refuses to leave the
  // symbols array empty (keeps at least one selected); the explicit
  // empty-list guard below catches any edge case where consumers reach
  // runBatch with an empty universe.

  /** Run study. A press while an earlier press is still starting its job is dropped. */
  runBatch(): Promise<void> {
    return this.gate.press(() => this.startBatch());
  }

  /** Sets the inputs from a saved run's. Everything is read first, so a field it cannot read leaves the page as it was. */
  private fillFrom(stored: StoredInputs): void {
    const symbols = stored.texts('symbols').map((symbol) => symbol.toUpperCase());
    if (symbols.length === 0) throw new StoredInputsError('"symbols" names no universe');
    const feature = stored.text('feature_name');
    const target = stored.text('target_type');
    if (!OPTIONS_FEATURES.some((option) => option.value === feature)) {
      throw new StoredInputsError(`"${feature}" is not a feature this page offers`);
    }
    if (!TARGET_TYPES.some((option) => option.value === target)) {
      throw new StoredInputsError(`"${target}" is not a target this page offers`);
    }
    this.range.update((range) => ({ ...range, symbols, from: stored.text('from_date'), to: stored.text('to_date') }));
    this.featureName.set(feature);
    this.targetType.set(target);
  }

  private async startBatch(): Promise<void> {
    if (this.range().symbols.length === 0) {
      this.error.set('Select at least one ticker');
      return;
    }

    this.error.set(null);
    this.result.set(null);
    this.inputs.clear();
    this.logBuffer.clear();

    try {
      // Wire the canonical MultiTickerRange shape via the shared adapter.
      // CrossSectionalJobRequest inherits MultiTickerRequest in PR (ii);
      // legacy 'tickers' alias still accepted until PR (iii)'s alias
      // removal commit, but we send canonical 'symbols' now.
      const submitted = this.enteredInputs();
      const wire = multiTickerRangeToWire({
        ...submitted.range,
        symbols: submitted.range.symbols.map((s) => s.toUpperCase()),
      });
      const parent = this.prefill.parent();
      const id = await this.jobsService.startJob('cross_sectional', {
        ...wire,
        feature_name: submitted.feature,
        target_type: submitted.target,
        ...(parent === null ? {} : { rerun_of: parent }),
      });
      this.prefill.started();
      this.submitted = submitted;
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

  async onPanelCancel(): Promise<void> {
    await this.cancelRun();
  }

  private async handleCompleted(id: string): Promise<void> {
    try {
      const raw = await this.jobsService.fetchResult<CrossSectionalJobResultRaw>(id);
      this.result.set(toBatchResearchResult(raw));
      if (this.submitted !== null) this.inputs.capture(this.submitted);
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to fetch cross-sectional result';
      this.error.set(msg);
    }
  }
}
