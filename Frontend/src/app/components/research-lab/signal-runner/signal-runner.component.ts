import {
  Component,
  signal,
  computed,
  inject,
  input,
  ChangeDetectionStrategy,
  effect,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';

import { SignalEngineResult } from '../../../services/research.service';
import { SignalEngineJobResultRaw, toSignalEngineResultFromJob } from './signal-result.mapper';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { SignalReportComponent } from '../signal-report/signal-report.component';
import { RunProgressPanelComponent, PhaseRailStop } from '../shared/run-progress-panel/run-progress-panel.component';
import { MessageModule } from 'primeng/message';
import { ToggleSwitch } from 'primeng/toggleswitch';
import { TagModule } from 'primeng/tag';
import {
  IndicatorPickerAdd,
  IndicatorPickerComponent,
} from '../../../shared/indicator-picker/indicator-picker.component';
import {
  IndicatorCatalogService,
} from '../../../shared/indicator-catalog/indicator-catalog.service';
import { findFeatureId, findMappingForFeature } from '../../../shared/indicator-catalog/feature-mapping';
import { ActiveIndicatorCardComponent } from '../../data-lab/active-indicator-card/active-indicator-card.component';
import { IndicatorConfigModalComponent } from '../../data-lab/indicator-config-modal/indicator-config-modal.component';
import { TickerRangePickerComponent } from '../../../shared/ticker-range-picker/ticker-range-picker.component';
import type {
  Resolution,
  TickerRange,
} from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import { tickerRangeToWire } from '../../../utils/ticker-wire';
import { StudyPageComponent } from '../../../research/study-page/study-page.component';
import { createRunGate } from '../../../research/run-gate';
import { createRerunPrefill } from '../../../research/stored-study/rerun';
import { StoredInputsError, type StoredInputs } from '../../../research/stored-study/stored-inputs';
import { createResultInputTracker } from '../../../shared/workspace/previous-result';

/** What a Signal study's request is made from: the inputs a shown result answers. */
interface SignalStudyInputs {
  readonly range: TickerRange;
  readonly indicator: string;
  readonly params: Record<string, number>;
  readonly flipSign: boolean;
  readonly regimeGate: boolean;
}

@Component({
  selector: 'app-signal-runner',
  imports: [
    CommonModule,
    FormsModule,
    SignalReportComponent,
    RunProgressPanelComponent,
    MessageModule,
    ToggleSwitch,
    TagModule,
    IndicatorPickerComponent,
    ActiveIndicatorCardComponent,
    IndicatorConfigModalComponent,
    TickerRangePickerComponent,
    StudyPageComponent,
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
  private catalog = inject(IndicatorCatalogService);

  /** `?rerun=<saved run id>`: fill the inputs from that run. It runs nothing; Run study is still the reader's. */
  readonly rerun = input<string>();
  private readonly prefill = createRerunPrefill({
    study: 'signal-study',
    id: this.rerun,
    apply: (stored) => this.fillFrom(stored),
  });
  readonly prefillNotice = this.prefill.notice;

  /** Catalog passed to the picker. */
  readonly catalogCategories = this.catalog.categories;
  readonly catalogLoading = this.catalog.loading;
  readonly catalogLoadFailed = this.catalog.failed;

  // Form inputs
  // CRITICAL — ``range.multiplier`` initialized to 15 to preserve
  // ``SignalEngineJobRequest``'s pre-migration default. Without this
  // override, the inherited ``TickerRequest`` base default of 1 would
  // silently switch every run from 15-minute to 1-minute bars. The
  // backend also pins multiplier=15 explicitly on its side (PR ii); the
  // frontend mirror keeps the UI consistent with that default.
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: '2024-01-02',
    to: '2024-06-28',
    resolution: 'minute',
    multiplier: 15,
  });
  /**
   * The target looks 15 bars ahead and needs 70% of the bars to have one, so a
   * bar of 30 minutes or longer can never reach it: hour, daily, ×60 and ×240
   * bars always fail (#3527), and are not offered.
   */
  readonly availableMultipliers: readonly number[] = [1, 5, 15];
  readonly availableResolutions: readonly Resolution[] = ['minute'];

  /** Catalog-driven selection: indicator key + params (matches data-lab's
   *  ``IndicatorEntry`` shape). The ``feature_name`` sent to the backend is
   *  derived from this via ``findFeatureId``. */
  selectedIndicator = signal<string>('rsi');
  selectedParams = signal<Record<string, number>>({ length: 14 });
  flipSign = signal(true);
  regimeGateEnabled = signal(true);
  forceRefresh = signal(false);

  /** Single-element activeKeys array fed to the picker so the chosen row
   *  shows the active rail + +1 badge. */
  readonly pickerActiveKeys = computed<readonly string[]>(
    () => [this.selectedIndicator()],
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

  /** One press starts one run: a second click before the job is accepted is dropped. */
  private readonly gate = createRunGate();
  readonly starting = this.gate.starting;

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<SignalStudyInputs>(() => ({
    range: this.range(),
    indicator: this.selectedIndicator(),
    params: this.selectedParams(),
    flipSign: this.flipSign(),
    regimeGate: this.regimeGateEnabled(),
  }));
  readonly inputs = createResultInputTracker(
    this.enteredInputs,
    ({ range, indicator, params, flipSign, regimeGate }) => ({
      key: JSON.stringify([
        range.symbol.toUpperCase(),
        range.from,
        range.to,
        range.resolution,
        range.multiplier ?? 1,
        indicator,
        params,
        flipSign,
        regimeGate,
      ]),
      facts: [
        { label: 'Instrument', value: range.symbol.toUpperCase() },
        { label: 'Window', value: `${range.from} to ${range.to}` },
        { label: 'Bars', value: `${range.multiplier ?? 1} minute` },
        { label: 'Indicator', value: `${indicator} ${JSON.stringify(params)}` },
        { label: 'Flip sign', value: flipSign ? 'on' : 'off' },
        { label: 'Regime gate', value: regimeGate ? 'on' : 'off' },
      ],
    }),
  );
  private submitted: SignalStudyInputs | null = null;

  canRun = computed(() => {
    const r = this.range();
    return (
      r.symbol.trim().length > 0 &&
      this.featureMapping() !== null &&
      r.from.trim().length > 0 &&
      r.to.trim().length > 0 &&
      !this.loading() &&
      !this.starting()
    );
  });

  get selectedFeatureLabel(): string {
    return this.featureMapping()?.label ?? this.selectedIndicator().toUpperCase();
  }

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => {
    const r = this.range();
    return `${this.selectedFeatureLabel} on ${r.symbol.toUpperCase()}, ${r.from} to ${r.to}`;
  });

  /** Why Run study cannot start, in words that point at the step to fix. */
  readonly blockedReason = computed<string | null>(() => {
    if (this.canRun() || this.loading() || this.starting()) return null;
    if (this.featureMapping() === null) return 'Pick an indicator the study supports.';
    return 'Choose an instrument and both dates.';
  });

  /** Anything past the idle state is on screen: a job, an error or a result. */
  readonly hasOutput = computed(
    () => this.job() !== null || this.error() !== null || this.result() !== null,
  );

  /** Sets the inputs from a saved run's. Everything is read first, so a field it cannot read leaves the page as it was. */
  private fillFrom(stored: StoredInputs): void {
    const name = stored.text('feature_name');
    const mapping = findMappingForFeature(name);
    if (mapping === null) throw new StoredInputsError(`"${name}" is not an indicator this page offers`);
    const multiplier = stored.number('multiplier');
    if (stored.text('timespan') !== 'minute' || !this.availableMultipliers.includes(multiplier)) {
      throw new StoredInputsError(`${multiplier} ${stored.text('timespan')} bars are not offered here`);
    }
    const flipSign = stored.boolean('flip_sign');
    const regimeGate = stored.boolean('regime_gate_enabled');
    this.range.set({
      symbol: stored.text('symbol').toUpperCase(),
      from: stored.text('from_date'),
      to: stored.text('to_date'),
      resolution: 'minute',
      multiplier,
    });
    this.selectedIndicator.set(mapping.indicator);
    this.selectedParams.set({ ...mapping.params });
    this.flipSign.set(flipSign);
    this.regimeGateEnabled.set(regimeGate);
  }

  /** Picker (add) in this host = single-select replace. */
  onPickerSelect(event: IndicatorPickerAdd): void {
    this.selectedIndicator.set(event.name);
    this.selectedParams.set({ ...event.params });
  }

  openConfigure(): void {
    this.configureModalOpen.set(true);
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
    // The picker is a renderer; the host owns the catalog fetch.
    void this.catalog.load();

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

  /** Run study. A press while an earlier press is still starting its job is dropped. */
  runSignalEngine(): Promise<void> {
    return this.gate.press(() => this.startSignalEngine());
  }

  private async startSignalEngine(): Promise<void> {
    this.error.set(null);
    this.result.set(null);
    this.inputs.clear();
    this.logBuffer.clear();

    try {
      const featureId = this.featureName();
      if (!featureId) {
        this.error.set(this.unsupportedReason() ?? 'No feature selected');
        return;
      }
      // Wire the canonical TickerRange shape via the shared adapter.
      // SignalEngineJobRequest preserves multiplier=15 default (PR ii);
      // the picker is also initialized to multiplier=15 above so the UI
      // and the wire agree.
      const submitted = this.enteredInputs();
      // The run reads extended-hours bars whatever the request says, and the saved run keeps what was
      // sent, so it says "extended" rather than the "rth" the wire would fill in.
      const wire = tickerRangeToWire({
        ...submitted.range,
        symbol: submitted.range.symbol.toUpperCase(),
        session: 'extended',
      });
      const parent = this.prefill.parent();
      const id = await this.jobsService.startJob('signal_engine', {
        ...wire,
        feature_name: featureId,
        flip_sign: submitted.flipSign,
        regime_gate_enabled: submitted.regimeGate,
        force: this.forceRefresh(),
        ...(parent === null ? {} : { rerun_of: parent }),
      });
      this.prefill.started();
      this.submitted = submitted;
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

  private async handleCompleted(id: string): Promise<void> {
    try {
      const raw = await this.jobsService.fetchResult<SignalEngineJobResultRaw>(id);
      const mapped = toSignalEngineResultFromJob(raw);
      this.result.set(mapped);
      if (mapped.success) {
        if (this.submitted !== null) this.inputs.capture(this.submitted);
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
}
