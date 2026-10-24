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
import { HttpErrorResponse } from '@angular/common/http';
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
  Session,
  TickerRange,
} from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import { tickerRangeToWire } from '../../../utils/ticker-wire';
import { StudyPageComponent } from '../../../research/study-page/study-page.component';
import { createRunGate } from '../../../research/run-gate';
import { createRerunPrefill } from '../../../research/stored-study/rerun';
import { StoredInputsError, type StoredInputs } from '../../../research/stored-study/stored-inputs';
import { createResultInputTracker } from '../../../shared/workspace/previous-result';
import type { DataSourceProvenance } from '../../../shared/workspace/data-source-provenance';
import { LakeStudySourceComponent } from '../../../shared/lake-study/lake-study-source.component';
import { LakeHoldingNoticeComponent } from '../../../shared/lake-study/lake-holding-notice.component';
import { createLakeHolding } from '../../../shared/lake-study/create-lake-holding';
import { storedSession } from '../../../shared/lake-study/stored-session';
import type { InstrumentDataRequest } from '../../../shared/data-lake';
import {
  LAKE_STUDY_TREE,
  readLakeStudyRefusal,
  SESSION_POLICY_FOR_REQUEST,
  sessionPolicyWords,
  toDataSourceProvenance,
  type LakeSessionPolicy,
  type LakeStudyRefusalShown,
} from '../../../shared/lake-study/lake-study';
import signalBarMinutes from '@repo-contracts/signal-study-bar-minutes.json';

/**
 * The bar lengths, in minutes, the server accepts for each session: the target looks 15 bars ahead inside the same day
 * and needs 70% of the bars to have one, so a regular session reaches that with shorter bars than an extended one does,
 * and hour, daily and longer minute bars with neither. A data-plane test pins this file to the study's own refusals
 * (`contracts/signal-study-bar-minutes.json`); the page works out nothing of its own.
 */
const BAR_MINUTES: Readonly<Record<Session, readonly number[]>> = signalBarMinutes;

/** The offered length nearest to `wanted`, the shorter of two equally near. */
function nearestBar(offered: readonly number[], wanted: number): number {
  return offered.reduce((best, length) => {
    const nearer = Math.abs(length - wanted) - Math.abs(best - wanted);
    return nearer < 0 || (nearer === 0 && length < best) ? length : best;
  });
}

/** What a Signal study's request is made from: the inputs a shown result answers. */
interface SignalStudyInputs {
  /** Always names its session and a bar length that session offers, so the wire never falls back to the adapter's `rth` default. */
  readonly range: TickerRange & { readonly session: Session };
  /** What the range's session asks the lake for. */
  readonly policy: LakeSessionPolicy;
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
    LakeStudySourceComponent,
    LakeHoldingNoticeComponent,
  ],
  templateUrl: './signal-runner.component.html',
  styleUrls: ['./signal-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalRunnerComponent {
  protected readonly signalEngineRail: readonly PhaseRailStop[] = [
    { label: 'Fetch',        ids: ['admitting_data', 'loading_bars'] },
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
  // frontend mirror keeps the UI consistent with that default. The session
  // is extended hours, which the study has always computed on, so a run
  // started without touching it moves no number (#3522).
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: '2024-01-02',
    to: '2024-06-28',
    resolution: 'minute',
    multiplier: 15,
    session: 'extended',
  });
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
  /**
   * Why the lake could not supply the window, when the run failed on that; read from the payload the failed job parked,
   * with the session it judged: the one its source records, or the one the run asked for when nothing was read.
   */
  readonly refusal = signal<LakeStudyRefusalShown | null>(null);

  /**
   * The data-source line for what is on screen: the shown result's lake source, or the source a refused run was
   * refused against. `undefined` before either exists, which leaves the study's own pre-run line.
   */
  readonly dataSource = computed<DataSourceProvenance | undefined>(() => {
    const source = this.result()?.dataSource ?? this.refusal()?.refused.dataSource ?? null;
    return source === null ? undefined : toDataSourceProvenance(source);
  });

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

  /**
   * The range the picker shows and the run sends. One that names no session is extended hours, which is what the study
   * computed on before it named one; one whose bar length the session does not offer reads as the nearest length it
   * does, so a refused length is never sent whichever way the range was entered. The choice entered stays in `range`,
   * and switching back to a session that offers it shows it again.
   */
  readonly shownRange = computed<TickerRange & { readonly session: Session }>(() => {
    const range = this.range();
    const session = range.session ?? 'extended';
    return { ...range, session, multiplier: nearestBar(BAR_MINUTES[session], range.multiplier ?? 1) };
  });

  /** The bar lengths the chosen session offers, from the server's contract. */
  readonly availableMultipliers = computed<readonly number[]>(() => BAR_MINUTES[this.shownRange().session]);

  /**
   * Which length the run uses when the one entered is not offered for the session, whichever session that is; and, under
   * regular hours, why the offered lengths are few.
   */
  readonly barNote = computed<string | null>(() => {
    const { session, multiplier } = this.shownRange();
    const entered = this.range().multiplier ?? 1;
    const changed = entered === multiplier ? '' : `${entered}-minute bars are not offered for this session, so this run uses ${multiplier}-minute bars.`;
    if (session !== 'rth') return changed === '' ? null : changed;
    const why =
      `Regular hours offer bars of ${this.availableMultipliers().join(', ')} minutes only: the study looks 15 bars ahead ` +
      'inside the same day and needs 70% of its bars to have a target, which a regular session holds too few longer bars to reach.';
    return changed === '' ? why : `${changed} ${why}`;
  });

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<SignalStudyInputs>(() => {
    const range = this.shownRange();
    return {
      range,
      policy: SESSION_POLICY_FOR_REQUEST[range.session],
      indicator: this.selectedIndicator(),
      params: this.selectedParams(),
      flipSign: this.flipSign(),
      regimeGate: this.regimeGateEnabled(),
    };
  });
  readonly inputs = createResultInputTracker(
    this.enteredInputs,
    ({ range, policy, indicator, params, flipSign, regimeGate }) => ({
      key: JSON.stringify([
        range.symbol.toUpperCase(),
        range.from,
        range.to,
        range.resolution,
        range.multiplier ?? 1,
        policy,
        indicator,
        params,
        flipSign,
        regimeGate,
      ]),
      facts: [
        { label: 'Instrument', value: range.symbol.toUpperCase() },
        { label: 'Window', value: `${range.from} to ${range.to}` },
        { label: 'Bars', value: `${range.multiplier ?? 1} minute` },
        { label: 'Session', value: sessionPolicyWords(policy) },
        { label: 'Indicator', value: `${indicator} ${JSON.stringify(params)}` },
        { label: 'Flip sign', value: flipSign ? 'on' : 'off' },
        { label: 'Regime gate', value: regimeGate ? 'on' : 'off' },
      ],
    }),
  );
  private submitted: SignalStudyInputs | null = null;

  /** The instrument and both dates are entered. */
  private readonly windowEntered = computed(() => {
    const r = this.range();
    return r.symbol.trim().length > 0 && r.from.trim().length > 0 && r.to.trim().length > 0;
  });

  canRun = computed(
    () => this.windowEntered() && this.featureMapping() !== null && !this.loading() && !this.starting(),
  );

  /**
   * How many of the run's sessions the lake does not hold yet, and the capture of them. The span is the one the run
   * admits: the entered window, in the split-adjusted tree (`SignalStudyParams.price_adjustment_mode`), with no warmup.
   * Read once per instrument or window; it warns and never holds up Run study.
   */
  readonly holding = createLakeHolding(
    computed<InstrumentDataRequest | null>(() => {
      if (!this.windowEntered()) return null;
      const r = this.range();
      return { symbol: r.symbol.trim().toUpperCase(), from: r.from, to: r.to, tree: LAKE_STUDY_TREE };
    }),
  );

  get selectedFeatureLabel(): string {
    return this.featureMapping()?.label ?? this.selectedIndicator().toUpperCase();
  }

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => {
    const { range, policy } = this.enteredInputs();
    return `${this.selectedFeatureLabel} on ${range.symbol.toUpperCase()}, ${range.from} to ${range.to}, ${sessionPolicyWords(policy)}`;
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
    const session = storedSession(stored, 'extended');
    if (stored.text('timespan') !== 'minute') {
      throw new StoredInputsError(`${multiplier} ${stored.text('timespan')} bars are not offered here`);
    }
    if (!BAR_MINUTES[session].includes(multiplier)) {
      throw new StoredInputsError(
        `${multiplier}-minute bars are not offered here for ${sessionPolicyWords(SESSION_POLICY_FOR_REQUEST[session])}`,
      );
    }
    const flipSign = stored.boolean('flip_sign');
    const regimeGate = stored.boolean('regime_gate_enabled');
    this.range.set({
      symbol: stored.text('symbol').toUpperCase(),
      from: stored.text('from_date'),
      to: stored.text('to_date'),
      resolution: 'minute',
      multiplier,
      session,
    });
    this.selectedIndicator.set(mapping.indicator);
    this.selectedParams.set({ ...mapping.params });
    this.flipSign.set(flipSign);
    this.regimeGateEnabled.set(regimeGate);
  }

  /**
   * What the picker handed over. It hands back the range it was shown, so a change that leaves the bar length alone (the
   * session, a date) must not commit the nearest length the shown range reads as in place of the one that was entered.
   */
  onRangeChange(next: TickerRange): void {
    this.range.set(next.multiplier === this.shownRange().multiplier ? { ...next, multiplier: this.range().multiplier } : next);
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
        void this.loadRefusal(id);
        this.holding.reread();
      } else if (j.status === 'cancelled' && resultFetchedFor !== id) {
        resultFetchedFor = id;
        this.error.set(j.message ?? 'Run cancelled');
        this.holding.reread();
      }
    });
  }

  /** Run study. A press while an earlier press is still starting its job is dropped. */
  runSignalEngine(): Promise<void> {
    return this.gate.press(() => this.startSignalEngine());
  }

  private async startSignalEngine(): Promise<void> {
    this.error.set(null);
    this.refusal.set(null);
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
      // The run admits exactly the session the person chose from the lake, and the saved run records it.
      const wire = tickerRangeToWire({ ...submitted.range, symbol: submitted.range.symbol.toUpperCase() });
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

  /**
   * A run the lake refused parks its reason and counts as the job's result (`status: "refused"`). A run that failed
   * for any other reason parks nothing, and the result route answers 404: the failure message already shown is all
   * there is. Any other failure to read the result is shown beside that message, never swallowed.
   */
  private async loadRefusal(id: string): Promise<void> {
    try {
      const refused = readLakeStudyRefusal(await this.jobsService.fetchResult<unknown>(id));
      if (refused !== null && this.jobId() === id) {
        this.refusal.set({ refused, session: refused.dataSource?.sessionPolicy ?? this.submitted?.policy ?? null });
      }
    } catch (err: unknown) {
      if (err instanceof HttpErrorResponse && err.status === 404) return;
      if (this.jobId() !== id) return;
      const detail =
        err instanceof HttpErrorResponse
          ? `HTTP ${err.status}`
          : err instanceof Error
            ? err.message
            : 'unknown error';
      this.error.update(
        (shown) => `${shown ?? 'Signal engine run failed'}. The lake's reason could not be loaded: ${detail}`,
      );
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
      // The run filled the lake with whatever it lacked, so the count of what it lacks is read again.
      this.holding.reread();
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to fetch signal engine result';
      this.error.set(msg);
    }
  }
}
