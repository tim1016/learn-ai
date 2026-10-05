import { DateRangePickerComponent, UNBOUNDED, type DateBounds } from '../../../shared/date-picker';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  output,
  signal,
  untracked,
} from '@angular/core';

import { ReceiptLabelPipe, formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { JobsService } from '../../../services/jobs.service';
import { BackfillRunLogComponent } from './backfill-run-log.component';
import { MultiInstrumentCardComponent } from '../../../shared/multi-ticker-range-picker/multi-instrument-card.component';
import {
  SymbolCatalogService,
  delistedVendorRows,
} from '../../../shared/symbol-catalog/symbol-catalog.service';
import type { PickerSymbol } from '../../../shared/symbol-catalog/symbol-catalog.types';
import {
  VendorCatalogService,
  LAKE_BACKFILLABLE_ASSET_CLASS,
} from '../../../shared/symbol-catalog/vendor-catalog.service';
import { toBackfillableMode } from '../../../shared/symbol-catalog/ensure-coverage.service';
import { etIsoDate } from '../../../shared/date/et-midnight';
import {
  DataLakeBackfillStore,
  type BackfillPhase,
  type BackfillRunEnd,
} from '../lib/data-lake-backfill.store';
import { BACKFILL_JOB_TYPE } from '../../../shared/data-lake/backfill-job-type';
import { needsAdjustmentRecord, type RebuildRequest } from '../lib/lake-readiness';
import {
  BackfillDefaults,
  DataLakeDataType,
  DataRunSpec,
  MAX_TRADING_RANGE_DAYS,
  PriceAdjustmentMode,
  tradingDateToMs,
  tradingRangeRejection,
} from '../../../shared/data-lake';

/**
 * Submits a backfill and narrates it to completion.
 *
 * The form starts on the window the page hands it; a rebuild fills in one
 * symbol's own window and runs it. Symbols are picked from the shared picker family, over the joined
 * catalog: the vendor's listing universe with the lake's coverage on each
 * row, so a dark vendor degrades to lake holdings under a banner instead
 * of an empty form. Delisted symbols sit behind an explicit toggle so a
 * survivorship-biased universe is an operator's visible choice. Progress
 * comes off the job's own SSE stream: a per-day tick plus the
 * `data_lake.backfill_day` domain event, whose typed `reason` codes reach
 * the operator through the receipt-label pipe rather than being re-worded.
 */
@Component({
  selector: 'app-lake-backfill-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lake-backfill-panel.component.html',
  styleUrl: './lake-backfill-panel.component.scss',
  imports: [ReceiptLabelPipe, BackfillRunLogComponent, MultiInstrumentCardComponent, DateRangePickerComponent],
  providers: [DataLakeBackfillStore],
})
export class LakeBackfillPanelComponent {
  protected readonly store = inject(DataLakeBackfillStore);
  private readonly jobs = inject(JobsService);
  private readonly vendor = inject(VendorCatalogService);
  private readonly symbols = inject(SymbolCatalogService);

  /** Null while the defaults read is in flight or the lake is dark. */
  readonly defaults = input<BackfillDefaults | null>(null);
  readonly seedStartTradingDate = input.required<string>();
  readonly seedEndTradingDate = input.required<string>();
  /**
   * The view the page is showing, which is the tree this panel's run writes.
   * `lean_adjusted` has no producer, so the panel refuses it rather than
   * leave that view exactly as empty as it started.
   */
  readonly priceAdjustmentMode = input<PriceAdjustmentMode>('raw');

  /** Fired once a run reaches any terminal phase, so the caller can re-read the lake. */
  readonly runFinished = output();

  /** True while a run is being submitted or is running. */
  readonly busy = computed(() => this.store.running());

  /** The symbols the operator has picked, or the one a rebuild filled in. */
  protected readonly pickedSymbols = signal<string[]>([]);
  protected readonly startTradingDate = linkedSignal(() => this.seedStartTradingDate());
  protected readonly endTradingDate = linkedSignal(() => this.seedEndTradingDate());
  protected readonly includeQuotes = signal(false);
  /**
   * Delisted symbols can still be backfilled (the vendor's history for them
   * persists), but offering them by default would let an unbiased-looking
   * universe accrete by accident. One toggle keeps it a decision.
   */
  protected readonly includeDelisted = signal(false);

  /**
   * The joined picker universe, read on the tree this panel's run writes.
   * When the vendor is dark the pool degrades to lake holdings on its own —
   * the vendor's answer is never a precondition for offering *something*.
   */
  protected readonly catalogView = computed(() => {
    // `viewFor` creates the mode's resource on first ask, and `resource()`
    // installs an effect — illegal inside a reactive context (NG0602).
    const mode = this.backfillableMode() ?? 'raw';
    return untracked(() => this.symbols.viewFor(mode));
  });

  /**
   * The picker's options: the joined pool, plus the vendor's delisted rows
   * only while the toggle says so. Held rows always stay — a delisted
   * symbol the lake holds has real bars, toggle or no toggle.
   */
  protected readonly pickerOptions = computed<readonly PickerSymbol[]>(() => {
    const pool = this.catalogView().pool();
    if (!this.includeDelisted()) return pool;
    const present = new Set(pool.map((row) => row.symbol));
    const extras = delistedVendorRows(this.vendor.entries() ?? []).filter(
      (row) => !present.has(row.symbol),
    );
    return [...pool, ...extras];
  });

  /**
   * Selected symbols the catalog cannot vouch for — delisted with the
   * toggle off, or a rebuild of a symbol the vendor no longer lists. Lake
   * membership alone has never made something backfillable; when the vendor
   * has answered, an unvouched pick blocks submission by name instead of
   * shipping a spec the pipeline refuses. When the vendor is dark no verdict
   * is possible, so nothing blocks — an outage may not silently disable the
   * Observatory's primary flow.
   */
  protected readonly ineligibleSelections = computed<readonly string[]>(() => {
    if (this.vendor.entries() === null) return [];
    const offerable = new Set(this.pickerOptions().map((row) => row.symbol));
    return this.pickedSymbols().filter((symbol) => !offerable.has(symbol));
  });

  protected retryVendorCatalog(): void {
    this.vendor.reload();
  }

  protected reloadCoverage(): void {
    this.catalogView().reload();
  }

  protected readonly digest = computed(() => this.defaults()?.lean_image_digest ?? null);

  protected readonly writesAdjustmentRecord = computed(() =>
    needsAdjustmentRecord(this.priceAdjustmentMode()),
  );

  /**
   * The requested view as a mode the fetch pipeline can actually write, or
   * `null` when it cannot. Narrowing here rather than asserting at the submit
   * site keeps `DataRunSpec` honest about which modes it accepts.
   */
  protected readonly backfillableMode = computed(() =>
    toBackfillableMode(this.priceAdjustmentMode()),
  );

  protected readonly blockedReason = computed<string | null>(() => {
    // Every reason submit is unavailable lives here, the capability check
    // included: a `request_id` is the run's durable identity, so a browser
    // that cannot mint one has no business reaching the submit path at all.
    if (!canMintRequestId()) return 'This browser cannot create a durable request identity.';
    if (this.defaults() === null) return 'Backfill is unavailable until the data plane answers.';
    if (this.digest() === null) {
      return 'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.';
    }
    if (this.backfillableMode() === null) {
      const mode = this.priceAdjustmentMode();
      return `Nothing derives the ${formatReceiptLabel(mode)} view, so a backfill cannot fill it — those rows arrive by import. Switch the view to Raw or Polygon Split Adjusted to backfill.`;
    }
    if (this.ineligibleSelections().length > 0) {
      return `Not in the listing catalog (a typo, a delisted symbol, or not a US stock): ${this.ineligibleSelections().join(', ')}. Clear them, or include delisted symbols.`;
    }
    if (this.pickedSymbols().length === 0) return 'Pick at least one symbol.';
    const rejection = tradingRangeRejection(
      this.startTradingDate(),
      this.endTradingDate(),
      this.defaults()?.max_trading_range_days ?? MAX_TRADING_RANGE_DAYS,
    );
    return rejection;
  });

  /**
   * The dates a backfill can ask for. The cap is not the only bound on a
   * window: a start before the provider serves aborts the whole run on its
   * oldest day (#2241), so the picker refuses one with the reason; the data
   * plane refuses it too. There is no last day: the data plane judges the end.
   */
  protected readonly dateBounds = computed<DateBounds>(() => {
    const floor = this.providerHistoryStart();
    if (floor === null) return UNBOUNDED;
    return { ...UNBOUNDED, min: floor, minReason: `The market-data provider serves no bars before ${floor}. Start on or after that date.` };
  });

  /** The oldest day the provider serves, as the data plane reports it. */
  protected readonly providerHistoryStart = computed<string | null>(() => {
    const ms = this.defaults()?.provider_history_start_ms;
    return typeof ms === 'number' && Number.isFinite(ms) ? etIsoDate(ms) : null;
  });

  protected readonly canSubmit = computed(
    () => this.blockedReason() === null && !this.store.running(),
  );

  protected readonly percent = computed(() => {
    const progress = this.store.progress();
    if (progress === null || progress.total <= 0) return 0;
    return Math.min(100, Math.round((progress.current / progress.total) * 100));
  });

  private lastSeenPhase: BackfillPhase = 'idle';

  constructor() {
    // This store is panel-scoped, so navigating away destroys it while the
    // worker keeps writing sessions. Adopt any backfill the jobs registry
    // still shows as live — matching on type alone, because a data-lake
    // backfill has no per-page scope the way a walk-forward job does, and
    // this is the only surface that starts one.
    effect(() => {
      if (this.store.phase() !== 'idle') return;
      const live = this.jobs
        .jobs()
        .find(
          (candidate) =>
            candidate.type === BACKFILL_JOB_TYPE &&
            (candidate.status === 'queued' || candidate.status === 'running'),
        );
      if (live !== undefined) this.store.reattach(live.id);
    });

    // `start()` resolves once the subscription is open, not when the run
    // ends — completion only ever arrives as an SSE frame. Watching the
    // store's phase is what lets the page re-read coverage the moment the
    // last session lands, instead of leaving a stale heatmap behind.
    //
    // Every terminal phase counts, not just success. A range that dies on
    // day 8 of 10 still put eight sessions on disk, and so does one the
    // operator cancels between per-day writes; leaving either invisible is
    // the same stale heatmap by another route.
    effect(() => {
      const phase = this.store.phase();
      const previous = this.lastSeenPhase;
      this.lastSeenPhase = phase;
      if (phase !== previous && TERMINAL_REREAD_PHASES.has(phase)) this.runFinished.emit();
    });

    // Clearing the delisted toggle is a statement about the universe, so
    // the delisted selections it made possible must not survive it —
    // otherwise the form submits delisted data while its own control says
    // not to include it. Only what the toggle alone had offered is dropped:
    // a delisted-but-held row stays offered (and selected) either way, and
    // merely unvouched picks stay for the block message to name, not as
    // silent deletions to hide.
    effect(() => {
      if (this.includeDelisted()) return;
      const pool = new Set(
        this.catalogView()
          .pool()
          .map((row) => row.symbol),
      );
      const droppable = new Set(
        (this.vendor.entries() ?? [])
          .filter(
            (entry) =>
              entry.asset_class === LAKE_BACKFILLABLE_ASSET_CLASS &&
              entry.status === 'inactive' &&
              !pool.has(entry.symbol),
          )
          .map((entry) => entry.symbol),
      );
      const current = this.pickedSymbols();
      const kept = current.filter((symbol) => !droppable.has(symbol));
      if (kept.length !== current.length) {
        this.pickedSymbols.set(kept);
      }
    });
  }

  protected onDates(dates: { readonly from: string; readonly to: string }): void {
    this.startTradingDate.set(dates.from);
    this.endTradingDate.set(dates.to);
  }

  protected onIncludeQuotes(event: Event): void {
    this.includeQuotes.set((event.target as HTMLInputElement).checked);
  }

  protected onIncludeDelisted(event: Event): void {
    this.includeDelisted.set((event.target as HTMLInputElement).checked);
  }

  /**
   * Fill the form with one symbol's rebuild window, run it, and resolve with
   * how the run ended.
   *
   * Resolves null when the form refuses the run; `blockedReason` then says
   * why in the panel, the same as for a hand-filled form.
   */
  async rebuild(request: RebuildRequest): Promise<BackfillRunEnd | null> {
    this.pickedSymbols.set([request.symbol]);
    this.startTradingDate.set(request.from);
    this.endTradingDate.set(request.to);
    this.includeQuotes.set(request.includeQuotes);
    const spec = this.composeSpec();
    return spec === null ? null : this.store.run(spec);
  }

  protected async submit(): Promise<void> {
    const spec = this.composeSpec();
    if (spec !== null) await this.store.start(spec);
  }

  /** The spec the form describes, or null while the form cannot be submitted. */
  private composeSpec(): DataRunSpec | null {
    const digest = this.digest();
    const defaults = this.defaults();
    const mode = this.backfillableMode();
    // `blockedReason`'s tradingRangeRejection() check already proved both
    // dates parse as YYYY-MM-DD before canSubmit() can be true, so these
    // are never null in practice — checked explicitly rather than asserted,
    // so a future change to that gate fails safe instead of sending NaN.
    const startMs = tradingDateToMs(this.startTradingDate());
    const endMs = tradingDateToMs(this.endTradingDate());
    if (
      digest === null ||
      defaults === null ||
      mode === null ||
      startMs === null ||
      endMs === null ||
      !this.canSubmit()
    ) {
      return null;
    }

    const dataTypes: DataLakeDataType[] = this.includeQuotes() ? ['trade', 'quote'] : ['trade'];
    return {
      // `canSubmit()` already proved `randomUUID` exists — see blockedReason.
      request_id: globalThis.crypto.randomUUID(),
      run_type: 'python_lab',
      market: defaults.market,
      symbols: this.pickedSymbols(),
      start_trading_date_ms: startMs,
      end_trading_date_ms: endMs,
      data_types: dataTypes,
      lean_image_digest: digest,
      // Backfill the view the operator is looking at. `blockedReason` has
      // already refused every mode the fetch pipeline cannot produce.
      price_adjustment_mode: mode,
    };
  }

  protected async cancel(): Promise<void> {
    await this.store.cancel();
  }
}

/**
 * Phases after which what is on disk may have changed.
 *
 * All three terminal phases qualify. `run_backfill` writes session by
 * session, so a run that failed or was cancelled part-way has still put
 * every session before that point in the catalog.
 */
const TERMINAL_REREAD_PHASES = new Set<BackfillPhase>(['completed', 'failed', 'cancelled']);

/** A backfill's `request_id` is its durable identity; refuse to invent a weak one. */
function canMintRequestId(): boolean {
  return typeof globalThis.crypto?.randomUUID === 'function';
}
