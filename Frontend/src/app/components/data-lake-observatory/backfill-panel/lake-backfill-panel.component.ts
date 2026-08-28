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
} from '@angular/core';

import { ReceiptLabelPipe, formatReceiptLabel } from '../../../shared/pipes/receipt-label.pipe';
import { JobsService } from '../../../services/jobs.service';
import { parseSymbols } from '../lib/coverage-board';
import { BackfillRunLogComponent } from './backfill-run-log.component';
import {
  BACKFILL_JOB_TYPE,
  DataLakeBackfillStore,
  type BackfillPhase,
} from '../lib/data-lake-backfill.store';
import type {
  BackfillDefaults,
  DataLakeDataType,
  DataRunSpec,
  PriceAdjustmentMode,
} from '../lib/data-lake.types';
import { MAX_TRADING_RANGE_DAYS, tradingRangeRejection } from '../lib/trading-range';

function inputValue(event: Event): string {
  return (event.target as HTMLInputElement).value;
}

/**
 * Submits a backfill and narrates it to completion.
 *
 * The form seeds itself from the window the heatmap is showing, so the
 * common move — "this stretch is missing, fetch it" — needs no retyping.
 * Progress comes off the job's own SSE stream: a per-day tick plus the
 * `data_lake.backfill_day` domain event, whose typed `reason` codes reach
 * the operator through the receipt-label pipe rather than being re-worded.
 */
@Component({
  selector: 'app-lake-backfill-panel',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lake-backfill-panel.component.html',
  styleUrl: './lake-backfill-panel.component.scss',
  imports: [ReceiptLabelPipe, BackfillRunLogComponent],
  providers: [DataLakeBackfillStore],
})
export class LakeBackfillPanelComponent {
  protected readonly store = inject(DataLakeBackfillStore);
  private readonly jobs = inject(JobsService);

  /** Null while the defaults read is in flight or the lake is dark. */
  readonly defaults = input<BackfillDefaults | null>(null);
  readonly seedSymbols = input<string>('');
  readonly seedStartTradingDate = input.required<string>();
  readonly seedEndTradingDate = input.required<string>();
  /**
   * The adjustment mode the heatmap is showing. The fetch pipeline only
   * ever writes `raw` rows (`DataRunSpec.price_adjustment_mode` is
   * `Literal["raw"]`), so a backfill run while an adjusted view is selected
   * would succeed and leave that view exactly as empty as it started.
   */
  readonly priceAdjustmentMode = input<PriceAdjustmentMode>('raw');

  /** Fired once a run reaches any terminal phase, so the caller can re-read coverage. */
  readonly runFinished = output();

  protected readonly symbolsText = linkedSignal(() => this.seedSymbols());
  protected readonly startTradingDate = linkedSignal(() => this.seedStartTradingDate());
  protected readonly endTradingDate = linkedSignal(() => this.seedEndTradingDate());
  protected readonly includeQuotes = signal(false);
  protected readonly forceRefresh = signal(false);

  protected readonly parsed = computed(() =>
    parseSymbols(this.symbolsText(), this.defaults()?.max_symbol_length ?? 20),
  );

  protected readonly digest = computed(() => this.defaults()?.lean_image_digest ?? null);

  protected readonly blockedReason = computed<string | null>(() => {
    // Every reason submit is unavailable lives here, the capability check
    // included: a `request_id` is the run's durable identity, so a browser
    // that cannot mint one has no business reaching the submit path at all.
    if (!canMintRequestId()) return 'This browser cannot create a durable request identity.';
    if (this.defaults() === null) return 'Backfill is unavailable until the data plane answers.';
    if (this.digest() === null) {
      return 'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.';
    }
    const mode = this.priceAdjustmentMode();
    if (mode !== 'raw') {
      return `The fetch pipeline writes raw bars only, so a backfill cannot fill the ${formatReceiptLabel(mode)} view — those rows arrive by import. Switch the view to Raw to backfill.`;
    }
    if (this.parsed().symbols.length === 0) return 'Enter at least one symbol.';
    return tradingRangeRejection(
      this.startTradingDate(),
      this.endTradingDate(),
      this.defaults()?.max_trading_range_days ?? MAX_TRADING_RANGE_DAYS,
    );
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
  }

  protected onSymbols(event: Event): void {
    this.symbolsText.set(inputValue(event));
  }

  protected onStart(event: Event): void {
    this.startTradingDate.set(inputValue(event));
  }

  protected onEnd(event: Event): void {
    this.endTradingDate.set(inputValue(event));
  }

  protected onIncludeQuotes(event: Event): void {
    this.includeQuotes.set((event.target as HTMLInputElement).checked);
  }

  protected onForceRefresh(event: Event): void {
    this.forceRefresh.set((event.target as HTMLInputElement).checked);
  }

  protected async submit(): Promise<void> {
    const digest = this.digest();
    const defaults = this.defaults();
    if (digest === null || defaults === null || !this.canSubmit()) return;

    const dataTypes: DataLakeDataType[] = this.includeQuotes() ? ['trade', 'quote'] : ['trade'];
    const spec: DataRunSpec = {
      // `canSubmit()` already proved `randomUUID` exists — see blockedReason.
      request_id: globalThis.crypto.randomUUID(),
      run_type: 'python_lab',
      market: defaults.market,
      symbols: this.parsed().symbols,
      start_trading_date: this.startTradingDate(),
      end_trading_date: this.endTradingDate(),
      data_types: dataTypes,
      lean_image_digest: digest,
      force_refresh: this.forceRefresh(),
    };
    await this.store.start(spec);
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
