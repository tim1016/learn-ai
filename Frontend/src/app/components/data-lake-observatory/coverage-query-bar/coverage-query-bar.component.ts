import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  untracked,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { parseSymbols } from '../lib/coverage-board';
import { DataLakeDataType, MAX_TRADING_RANGE_DAYS, PriceAdjustmentMode, tradingRangeRejection } from '../../../shared/data-lake';
import { SymbolCatalogService } from '../../../shared/symbol-catalog/symbol-catalog.service';
import { MultiInstrumentCardComponent } from '../../../shared/multi-ticker-range-picker/multi-instrument-card.component';

export interface ObservatoryQuery {
  readonly symbolsText: string;
  /** `YYYY-MM-DD`, the shape the coverage endpoint's `date` params take. */
  readonly startTradingDate: string;
  readonly endTradingDate: string;
  readonly dataType: DataLakeDataType;
  readonly priceAdjustmentMode: PriceAdjustmentMode;
}

export const DATA_TYPE_OPTIONS: readonly DataLakeDataType[] = ['trade', 'quote'];
export const PRICE_ADJUSTMENT_OPTIONS: readonly PriceAdjustmentMode[] = [
  'raw',
  'polygon_split_adjusted',
  'lean_adjusted',
];

function selectValue(event: Event): string {
  return (event.target as HTMLSelectElement).value;
}

function inputValue(event: Event): string {
  return (event.target as HTMLInputElement).value;
}

/**
 * The window the heatmap answers for.
 *
 * Free-text edits (symbols, dates) stay local until "Load coverage" is
 * pressed: the coverage endpoint issues one request per symbol over a range
 * capped at five years, so re-querying on every keystroke would be a lot of
 * traffic for a half-typed ticker. A `<select>` has no half-typed state —
 * choosing an option is one discrete action, the same shape as clicking
 * "Load coverage" itself — so data type and price adjustment apply the
 * instant they change. Without this, the backfill panel (seeded from the
 * applied query, per its own doc comment) silently keeps backfilling the
 * previously-applied mode while the dropdown already shows the new one.
 * The applied query is what the page — and the backfill form it seeds —
 * actually acts on.
 */
@Component({
  selector: 'app-coverage-query-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './coverage-query-bar.component.html',
  styleUrl: './coverage-query-bar.component.scss',
  imports: [ReceiptLabelPipe, MultiInstrumentCardComponent],
})
export class CoverageQueryBarComponent {
  readonly initial = input.required<ObservatoryQuery>();
  readonly maxSymbolLength = input(20);
  /** The data plane's own window cap; the coverage endpoint 422s past it. */
  readonly maxTradingRangeDays = input(MAX_TRADING_RANGE_DAYS);
  readonly busy = input(false);

  readonly applied = output<ObservatoryQuery>();

  protected readonly dataTypeOptions = DATA_TYPE_OPTIONS;
  protected readonly priceAdjustmentOptions = PRICE_ADJUSTMENT_OPTIONS;

  protected readonly draft = linkedSignal(() => this.initial());

  /**
   * The symbols the card holds, as chips. The card is the query's
   * membership surface over the joined catalog (ADR 0066) — but deliberately
   * WITHOUT the ensure-coverage gate: querying coverage is a read-only ask,
   * "not held" is a truthful heatmap answer, and the backfill panel this
   * bar seeds remains the populate path. `symbolsText` stays the query's
   * wire shape (URL seeding included); the chips are its editable form.
   */
  protected readonly chips = computed<string[]>(() => [
    ...parseSymbols(this.draft().symbolsText, this.maxSymbolLength()).symbols,
  ]);

  private readonly symbols = inject(SymbolCatalogService);

  /** Coverage badges follow the tree the heatmap will answer for. */
  private readonly catalogView = computed(() => {
    // `viewFor` installs a resource — step outside tracking (NG0602).
    const mode = this.draft().priceAdjustmentMode;
    return untracked(() => this.symbols.viewFor(mode));
  });

  protected readonly pickerOptions = computed(() => this.catalogView().pool());
  protected readonly pickerLoading = computed(
    () => this.catalogView().status().kind === 'loading',
  );
  protected readonly pickerUnavailable = computed<string | null>(() => {
    const status = this.catalogView().status();
    return status.kind === 'unavailable' ? status.message : null;
  });
  protected readonly pickerDegraded = computed<string | null>(() => {
    const status = this.catalogView().status();
    return status.kind === 'degraded' ? status.message : null;
  });

  protected readonly parsed = computed(() =>
    parseSymbols(this.draft().symbolsText, this.maxSymbolLength()),
  );

  protected readonly rangeRejection = computed(() => {
    const { startTradingDate, endTradingDate } = this.draft();
    return tradingRangeRejection(startTradingDate, endTradingDate, this.maxTradingRangeDays());
  });

  protected readonly canApply = computed(
    () => this.parsed().symbols.length > 0 && this.rangeRejection() === null,
  );

  protected onChips(symbols: string[]): void {
    this.patch({ symbolsText: symbols.join(', ') });
  }

  protected retryCoverage(): void {
    this.catalogView().reload();
  }

  protected retryVendorCatalog(): void {
    this.catalogView().retryVendor();
  }

  protected onStart(event: Event): void {
    this.patch({ startTradingDate: inputValue(event) });
  }

  protected onEnd(event: Event): void {
    this.patch({ endTradingDate: inputValue(event) });
  }

  protected onDataType(event: Event): void {
    this.patch({ dataType: selectValue(event) as DataLakeDataType });
    this.applyIfReady();
  }

  protected onPriceAdjustment(event: Event): void {
    this.patch({ priceAdjustmentMode: selectValue(event) as PriceAdjustmentMode });
    this.applyIfReady();
  }

  protected apply(): void {
    this.applyIfReady();
  }

  private applyIfReady(): void {
    if (!this.canApply()) return;
    this.applied.emit({ ...this.draft(), symbolsText: this.parsed().symbols.join(', ') });
  }

  private patch(change: Partial<ObservatoryQuery>): void {
    this.draft.update((current) => ({ ...current, ...change }));
  }
}
