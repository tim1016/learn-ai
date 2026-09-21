import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  model,
  untracked,
} from '@angular/core';
import { CommonModule } from '@angular/common';

import {
  TimeWindowCardComponent,
  type LegendTreatment,
} from '../ticker-range-picker/parts/time-window-card.component';
import {
  SamplingCardComponent,
  type SessionMode,
} from '../ticker-range-picker/parts/sampling-card.component';
import type {
  Resolution,
  TickerRange,
} from '../ticker-range-picker/ticker-range-picker.types';
import { DEFAULT_ADJUSTMENT_MODE } from '../ticker-catalog';
import { SymbolCatalogService } from '../symbol-catalog/symbol-catalog.service';
import type { PickerSymbol } from '../symbol-catalog/symbol-catalog.types';
import type { PriceAdjustmentMode } from '../data-lake';
import { MultiInstrumentCardComponent } from './multi-instrument-card.component';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

/**
 * Sibling of <app-ticker-range-picker> for a *universe* of symbols.
 *
 * Reuses the canonical picker's TimeWindow + Sampling sub-components
 * via a TickerRange projection — those sub-components don't know about
 * the universe shape; they two-way-bind a single-symbol TickerRange,
 * and this composer projects/applies the per-call patches onto the
 * MultiTickerRange.
 *
 * The universe is the joined catalog (ADR 0066): the vendor's listing
 * walk with the lake's coverage on each row, and an unheld pick gated on
 * its backfill inside the card — a batch over symbols the lake cannot
 * read would refuse its own data otherwise. Hosts name the tree their
 * run reads through `adjustmentMode`.
 */
@Component({
  selector: 'app-multi-ticker-range-picker',
  imports: [
    CommonModule,
    MultiInstrumentCardComponent,
    TimeWindowCardComponent,
    SamplingCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './multi-ticker-range-picker.component.html',
  styleUrls: ['./multi-ticker-range-picker.component.scss'],
})
export class MultiTickerRangePickerComponent {
  readonly value = model.required<MultiTickerRange>();
  /** The lake tree this picker's run reads; coverage and gating follow it. */
  readonly adjustmentMode = input<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);
  readonly availableResolutions = input<readonly Resolution[]>([
    'minute',
    'hour',
    'daily',
  ]);
  readonly availableMultipliers = input<readonly number[]>([]);
  readonly hideSampling = input(false);
  readonly sessionMode = input<SessionMode>('preview');
  readonly showAutoFetch = input(true);
  readonly title = input('Cross-sectional data');
  readonly legendTreatment = input<LegendTreatment>('tinted-bold');

  private readonly symbols = inject(SymbolCatalogService);
  protected readonly view = computed(() => {
    // `viewFor` creates the mode's resource on first ask, and `resource()`
    // installs an effect — illegal inside a reactive context (NG0602).
    const mode = this.adjustmentMode();
    return untracked(() => this.symbols.viewFor(mode));
  });

  /** The joined universe, adapted into the multi card's typed options. */
  protected readonly options = computed<readonly PickerSymbol[]>(() =>
    this.view().pool(),
  );

  protected retryCatalog(): void {
    this.view().reload();
  }

  protected retryVendorCatalog(): void {
    this.view().retryVendor();
  }

  protected setSymbols(symbols: string[]): void {
    this.value.set({ ...this.value(), symbols });
  }

  /** Project the universe onto a single-symbol TickerRange shape so
   *  the shared TimeWindow + Sampling sub-components can consume it
   *  without knowing about the universe API. */
  protected readonly singleProjection = computed<TickerRange>(() => {
    const v = this.value();
    return {
      symbol: v.symbols[0] ?? '',
      from: v.from,
      to: v.to,
      resolution: v.resolution,
      multiplier: v.multiplier,
      session: v.session,
      autoFetch: v.autoFetch,
    };
  });

  protected onSinglePatch(updated: TickerRange): void {
    const v = this.value();
    this.value.set({
      ...v,
      from: updated.from,
      to: updated.to,
      resolution: updated.resolution,
      multiplier: updated.multiplier,
      session: updated.session,
      autoFetch: updated.autoFetch,
    });
  }
}
