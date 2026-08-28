import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  linkedSignal,
  output,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { parseSymbols } from '../lib/coverage-board';
import type { DataLakeDataType, PriceAdjustmentMode } from '../lib/data-lake.types';

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
 * Edits stay local until "Load coverage" is pressed: the coverage endpoint
 * issues one request per symbol over a range capped at five years, so
 * re-querying on every keystroke would be a lot of traffic for a half-typed
 * ticker. The applied query is what the page — and the backfill form it
 * seeds — actually acts on.
 */
@Component({
  selector: 'app-coverage-query-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './coverage-query-bar.component.html',
  styleUrl: './coverage-query-bar.component.scss',
  imports: [ReceiptLabelPipe],
})
export class CoverageQueryBarComponent {
  readonly initial = input.required<ObservatoryQuery>();
  readonly maxSymbolLength = input(20);
  readonly busy = input(false);

  readonly applied = output<ObservatoryQuery>();

  protected readonly dataTypeOptions = DATA_TYPE_OPTIONS;
  protected readonly priceAdjustmentOptions = PRICE_ADJUSTMENT_OPTIONS;

  protected readonly draft = linkedSignal(() => this.initial());

  protected readonly parsed = computed(() =>
    parseSymbols(this.draft().symbolsText, this.maxSymbolLength()),
  );

  protected readonly invalidRange = computed(() => {
    const { startTradingDate, endTradingDate } = this.draft();
    return startTradingDate === '' || endTradingDate === '' || startTradingDate > endTradingDate;
  });

  protected readonly canApply = computed(
    () => this.parsed().symbols.length > 0 && !this.invalidRange(),
  );

  protected onSymbols(event: Event): void {
    this.patch({ symbolsText: inputValue(event) });
  }

  protected onStart(event: Event): void {
    this.patch({ startTradingDate: inputValue(event) });
  }

  protected onEnd(event: Event): void {
    this.patch({ endTradingDate: inputValue(event) });
  }

  protected onDataType(event: Event): void {
    this.patch({ dataType: selectValue(event) as DataLakeDataType });
  }

  protected onPriceAdjustment(event: Event): void {
    this.patch({ priceAdjustmentMode: selectValue(event) as PriceAdjustmentMode });
  }

  protected apply(): void {
    if (!this.canApply()) return;
    this.applied.emit({ ...this.draft(), symbolsText: this.parsed().symbols.join(', ') });
  }

  private patch(change: Partial<ObservatoryQuery>): void {
    this.draft.update((current) => ({ ...current, ...change }));
  }
}
