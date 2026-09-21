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
import { DEFAULT_ADJUSTMENT_MODE, TickerCatalogService } from '../ticker-catalog';
import type { PickerSymbol } from '../symbol-catalog/symbol-catalog.types';
import { toPickerSymbol } from '../symbol-catalog/symbol-catalog.types';
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
 * Out of v1: per-ticker availability strip, smart advisories, cache
 * hint. Multi-ticker UX for those is a separate UX problem.
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

  private readonly catalog = inject(TickerCatalogService);
  private readonly lakeView = computed(() => {
    // `viewFor` creates the mode's resource on first ask, and `resource()`
    // installs an effect — illegal inside a reactive context (NG0602).
    return untracked(() => this.catalog.viewFor(DEFAULT_ADJUSTMENT_MODE));
  });

  /** The lake universe, adapted into the multi card's typed options. */
  protected readonly options = computed<readonly PickerSymbol[]>(() =>
    this.lakeView().pool().map(toPickerSymbol),
  );
  protected readonly catalogLoading = computed(() => this.lakeView().loading());
  protected readonly catalogUnavailable = computed(() => this.lakeView().unavailable());

  protected retryCatalog(): void {
    this.lakeView().reload();
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
