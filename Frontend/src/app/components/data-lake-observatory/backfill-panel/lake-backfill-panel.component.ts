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

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { parseSymbols } from '../lib/coverage-board';
import { BackfillRunLogComponent } from './backfill-run-log.component';
import { DataLakeBackfillStore, type BackfillPhase } from '../lib/data-lake-backfill.store';
import type { BackfillDefaults, DataLakeDataType, DataRunSpec } from '../lib/data-lake.types';

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

  /** Null while the defaults read is in flight or the lake is dark. */
  readonly defaults = input<BackfillDefaults | null>(null);
  readonly seedSymbols = input<string>('');
  readonly seedStartTradingDate = input.required<string>();
  readonly seedEndTradingDate = input.required<string>();

  /** Fired on a terminal, non-cancelled run so the caller can re-read coverage. */
  readonly runFinished = output();

  protected readonly symbolsText = linkedSignal(() => this.seedSymbols());
  protected readonly startTradingDate = linkedSignal(() => this.seedStartTradingDate());
  protected readonly endTradingDate = linkedSignal(() => this.seedEndTradingDate());
  protected readonly includeQuotes = signal(false);
  protected readonly forceRefresh = signal(false);
  protected readonly submitError = signal<string | null>(null);

  protected readonly parsed = computed(() =>
    parseSymbols(this.symbolsText(), this.defaults()?.max_symbol_length ?? 20),
  );

  protected readonly digest = computed(() => this.defaults()?.lean_image_digest ?? null);

  protected readonly blockedReason = computed<string | null>(() => {
    if (this.defaults() === null) return 'Backfill is unavailable until the data plane answers.';
    if (this.digest() === null) {
      return 'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.';
    }
    if (this.parsed().symbols.length === 0) return 'Enter at least one symbol.';
    if (this.startTradingDate() === '' || this.endTradingDate() === '') return 'Pick a date range.';
    if (this.startTradingDate() > this.endTradingDate()) {
      return 'The start date is after the end date.';
    }
    return null;
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
    // `start()` resolves once the subscription is open, not when the run
    // ends — completion only ever arrives as an SSE frame. Watching the
    // store's phase is what lets the page re-read coverage the moment the
    // last session lands, instead of leaving a stale heatmap behind.
    effect(() => {
      const phase = this.store.phase();
      if (phase === 'completed' && this.lastSeenPhase !== 'completed') this.runFinished.emit();
      this.lastSeenPhase = phase;
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
    this.submitError.set(null);

    let requestId: string;
    try {
      requestId = newRequestId();
    } catch (error) {
      this.submitError.set(error instanceof Error ? error.message : String(error));
      return;
    }

    const dataTypes: DataLakeDataType[] = this.includeQuotes() ? ['trade', 'quote'] : ['trade'];
    const spec: DataRunSpec = {
      request_id: requestId,
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

/** A backfill's `request_id` is its durable identity; refuse to invent a weak one. */
function newRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID !== 'function') {
    throw new Error('This browser cannot create a durable request identity.');
  }
  return globalThis.crypto.randomUUID();
}
