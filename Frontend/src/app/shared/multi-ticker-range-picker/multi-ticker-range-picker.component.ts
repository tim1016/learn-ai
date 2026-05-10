import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
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
  TickerOption,
  TickerRange,
} from '../ticker-range-picker/ticker-range-picker.types';
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
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);
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
