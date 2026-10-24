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

import { ResearchResult } from '../../../services/research.service';
import { FeatureResearchJobResultRaw, toResearchResult } from './feature-result.mapper';
import { JobsService, JobState } from '../../../services/jobs.service';
import {
  glyphForLevel,
  pythonLevelToEntryLevel,
  RunLogBuffer,
} from '../../../utils/run-log-buffer';
import { FeatureReportComponent } from '../feature-report/feature-report.component';
import { RunProgressPanelComponent } from '../shared/run-progress-panel/run-progress-panel.component';
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
import { storedSession } from '../../../shared/lake-study/stored-session';
import featureBarMinutes from '@repo-contracts/feature-study-bar-minutes.json';

/** What a Feature study's request is made from: the inputs a shown result answers. */
interface FeatureStudyInputs {
  /** Always names its session, so the wire never falls back to the adapter's `rth` default. */
  readonly range: TickerRange & { readonly session: Session };
  /** What the range's session asks the lake for. */
  readonly policy: LakeSessionPolicy;
  readonly indicator: string;
  readonly params: Record<string, number>;
}

@Component({
  selector: 'app-feature-runner',
  imports: [
    CommonModule,
    FormsModule,
    FeatureReportComponent,
    RunProgressPanelComponent,
    MessageModule,
    CheckboxModule,
    TagModule,
    IndicatorPickerComponent,
    ActiveIndicatorCardComponent,
    IndicatorConfigModalComponent,
    TickerRangePickerComponent,
    StudyPageComponent,
    LakeStudySourceComponent,
    LakeHoldingNoticeComponent,
  ],
  templateUrl: './feature-runner.component.html',
  styleUrls: ['./feature-runner.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class FeatureRunnerComponent {
  private jobsService = inject(JobsService);
  private catalog = inject(IndicatorCatalogService);

  /** `?rerun=<saved run id>`: fill the inputs from that run. It runs nothing; Run study is still the reader's. */
  readonly rerun = input<string>();
  private readonly prefill = createRerunPrefill({
    study: 'feature-study',
    id: this.rerun,
    apply: (stored) => this.fillFrom(stored),
  });
  readonly prefillNotice = this.prefill.notice;

  /** Catalog passed to the picker. Exposed as a getter so the template can
   *  read the service signal directly without leaking the service. */
  readonly catalogCategories = this.catalog.categories;
  readonly catalogLoading = this.catalog.loading;
  readonly catalogLoadFailed = this.catalog.failed;

  // Form inputs
  // Single TickerRange replaces (ticker, fromDate, toDate, timespan,
  // multiplier). multiplier=1 matches FeatureResearchJobRequest's
  // pre-migration default; the base TickerRequest also defaults to 1
  // so no behavior change. The session is extended hours, which the study has
  // always computed on, so a run started without touching it moves no number (#3521).
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: '2024-01-02',
    to: '2024-03-28',
    resolution: 'minute',
    multiplier: 1,
    session: 'extended',
  });
  /**
   * The bar lengths the study accepts: the study looks 15 minutes ahead, so a bar must divide 15 minutes, and hour,
   * daily, ×60 and ×240 bars always fail (#3527). The list is the server's `SUPPORTED_BAR_MINUTES`, which a data-plane
   * test pins to this file (`contracts/fixtures`).
   */
  readonly availableMultipliers: readonly number[] = featureBarMinutes.bar_minutes;
  readonly availableResolutions: readonly Resolution[] = ['minute'];

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
  /**
   * Why the lake could not supply the window, when the run failed on that; read from the payload the failed job
   * parked, with the session it judged: the one its source records, or the one the run asked for when nothing was read.
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
   * The range the picker shows: one that names no session is extended hours, which is what the study computed on before
   * it named one. The picker shows the same session the run sends, instead of its own `rth` fallback.
   */
  readonly shownRange = computed<TickerRange & { readonly session: Session }>(() => {
    const range = this.range();
    return { ...range, session: range.session ?? 'extended' };
  });

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<FeatureStudyInputs>(() => {
    const range = this.shownRange();
    return {
      range,
      policy: SESSION_POLICY_FOR_REQUEST[range.session],
      indicator: this.selectedIndicator(),
      params: this.selectedParams(),
    };
  });
  readonly inputs = createResultInputTracker(this.enteredInputs, ({ range, policy, indicator, params }) => ({
    key: JSON.stringify([range.symbol.toUpperCase(), range.from, range.to, range.resolution, range.multiplier ?? 1, policy, indicator, params]),
    facts: [
      { label: 'Instrument', value: range.symbol.toUpperCase() },
      { label: 'Window', value: `${range.from} to ${range.to}` },
      { label: 'Bars', value: `${range.multiplier ?? 1} minute` },
      { label: 'Session', value: sessionPolicyWords(policy) },
      { label: 'Indicator', value: `${indicator} ${JSON.stringify(params)}` },
    ],
  }));
  private submitted: FeatureStudyInputs | null = null;

  // ── Range guard ─────────────────────────────────────
  // A minute-bar study is capped at 180 calendar days. The cap is older than the
  // lake path (it was sized to the provider fetch's connection window) and is kept
  // as it was; minute bars are the only bars this study offers (#3527).
  private static readonly MAX_RANGE_DAYS = 180;

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
    const cap = FeatureRunnerComponent.MAX_RANGE_DAYS;
    if (days <= cap) return null;
    return `Minute-bar research is capped at ${cap} days (you selected ${days} days). Shorten the range.`;
  });

  /** The instrument and a window the study can run on are both entered. */
  private readonly windowEntered = computed(() => {
    const r = this.range();
    return r.symbol.trim().length > 0 && r.from.trim().length > 0 && r.to.trim().length > 0 && this.rangeWarning() === null;
  });

  canRun = computed(
    () => this.windowEntered() && this.featureMapping() !== null && !this.loading() && !this.starting(),
  );

  /**
   * How many of the run's sessions the lake does not hold yet, and the capture of them. The span is the one the run
   * admits: the entered window, in the split-adjusted tree (`FeatureStudyParams.price_adjustment_mode`), with no warmup.
   * Read once per instrument or window; it warns and never holds up Run study.
   */
  readonly holding = createLakeHolding(
    computed<InstrumentDataRequest | null>(() => {
      if (!this.windowEntered()) return null;
      const r = this.range();
      return { symbol: r.symbol.trim().toUpperCase(), from: r.from, to: r.to, tree: LAKE_STUDY_TREE };
    }),
  );

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

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => {
    const { range, policy } = this.enteredInputs();
    return `${this.selectedFeatureLabel} on ${range.symbol.toUpperCase()}, ${range.from} to ${range.to}, ${sessionPolicyWords(policy)}`;
  });

  /** Why Run study cannot start, in words that point at the step to fix. */
  readonly blockedReason = computed<string | null>(() => {
    if (this.canRun() || this.loading() || this.starting()) return null;
    if (this.rangeWarning() !== null) return 'Shorten the window to run.';
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
    this.range.set({
      symbol: stored.text('symbol').toUpperCase(),
      from: stored.text('from_date'),
      to: stored.text('to_date'),
      resolution: 'minute',
      multiplier,
      // A run saved before it named a session computed on extended hours.
      session: storedSession(stored, 'extended'),
    });
    this.selectedIndicator.set(mapping.indicator);
    this.selectedParams.set({ ...mapping.params });
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
        void this.loadRefusal(id);
        this.holding.reread();
      } else if (j.status === 'cancelled' && this.resultSettledFor !== id) {
        this.resultSettledFor = id;
        this.error.set(j.message ?? 'Run cancelled');
        this.holding.reread();
      }
    });
  }

  /** Run study. A press while an earlier press is still starting its job is dropped. */
  runResearch(): Promise<void> {
    return this.gate.press(() => this.startResearch());
  }

  private async startResearch(): Promise<void> {
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
      // Python's FeatureResearchJobRequest inherits TickerRequest in PR
      // (ii); legacy aliases are still accepted until PR (iii)'s
      // alias-removal commit.
      const submitted = this.enteredInputs();
      // The run admits exactly the session the person chose from the lake, and the saved run records it.
      const wire = tickerRangeToWire({ ...submitted.range, symbol: submitted.range.symbol.toUpperCase() });
      const parent = this.prefill.parent();
      const id = await this.jobsService.startJob('feature_research', {
        ...wire,
        feature_name: featureId,
        force: this.forceRun(),
        ...(parent === null ? {} : { rerun_of: parent }),
      });
      this.prefill.started();
      this.submitted = submitted;
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
        (shown) => `${shown ?? 'Feature research run failed'}. The lake's reason could not be loaded: ${detail}`,
      );
    }
  }

  private async handleCompleted(id: string): Promise<void> {
    try {
      const raw = await this.jobsService.fetchResult<FeatureResearchJobResultRaw>(id);
      const mapped = toResearchResult(raw);
      this.result.set(mapped);
      if (mapped.success) {
        if (this.submitted !== null) this.inputs.capture(this.submitted);
      } else if (mapped.error) {
        this.error.set(mapped.error);
      }
      // Only mark this id as settled once the fetch actually returned;
      // a transient ``fetchResult`` failure should leave the guard open
      // so the next event tick can retry.
      this.resultSettledFor = id;
      // The run filled the lake with whatever it lacked, so the count of what it lacks is read again.
      this.holding.reread();
    } catch (err: unknown) {
      const msg =
        err && typeof err === 'object' && 'message' in err
          ? String((err as { message: unknown }).message)
          : 'Failed to fetch research result';
      this.error.set(msg);
    }
  }
}
