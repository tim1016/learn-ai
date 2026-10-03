import { ChangeDetectionStrategy, Component, computed, inject, input, model } from '@angular/core';

import { ProviderBoundsService, type DateBounds } from '../date-picker';
import type { PriceAdjustmentMode } from '../data-lake';
import { DEFAULT_ADJUSTMENT_MODE } from '../ticker-catalog';
import { InstrumentCardComponent } from './parts/instrument-card.component';
import { SamplingCardComponent, type SessionMode } from './parts/sampling-card.component';
import { TimeWindowCardComponent } from './parts/time-window-card.component';
import {
  computeAdvisories,
  type Advisory,
  type AdvisoryAction,
  type Resolution,
  type TickerOption,
  type TickerRange,
} from './ticker-range-picker.types';

export type { SessionMode };
type TickerRangePickerPresentation = 'card' | 'embedded';

/**
 * Shared ticker + range picker: the one way a page picks an instrument and
 * its dates together.
 *
 * Composes three child components under ``parts/``:
 *   <app-instrument-card>   — symbol combobox + dropdown + held span
 *   <app-time-window-card>  — the shared date range picker
 *   <app-sampling-card>     — resolution toggle + opt-in multiplier + session + auto-fetch
 *
 * Owns the cross-card concerns: the optional outer card frame, the summary
 * header and the advisory cluster. The host states what bounds its dates,
 * because only the host knows what its run reads.
 */
@Component({
  selector: 'app-ticker-range-picker',
  imports: [InstrumentCardComponent, TimeWindowCardComponent, SamplingCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './ticker-range-picker.component.html',
  styleUrls: ['./ticker-range-picker.component.scss'],
})
export class TickerRangePickerComponent {
  private readonly provider = inject(ProviderBoundsService);

  /** A host-supplied universe, for pickers whose subject is not lake bars
   *  (a live options snapshot, say). `null` means "ask the lake". */
  readonly universe = input<readonly TickerOption[] | null>(null);
  /** The lake tree this page's run will read. */
  readonly adjustmentMode = input<PriceAdjustmentMode>(DEFAULT_ADJUSTMENT_MODE);
  readonly value = model.required<TickerRange>();
  /**
   * What bounds the dates. Required, because a wrong guess offers dates the
   * run cannot read. ``provider`` is a run that fetches its own bars, or
   * materializes the missing days before it reads: the provider's oldest day
   * to the last completed session. Any other host hands over its own bound.
   */
  readonly dateBounds = input.required<'provider' | DateBounds>();
  /** Whether both dates must be trading sessions. */
  readonly sessionsOnly = input(true);

  readonly availableResolutions = input<readonly Resolution[]>(['minute', 'hour', 'daily']);
  readonly availableMultipliers = input<readonly number[]>([]);

  readonly showAutoFetch = input(true);

  /** Hides the entire Sampling card. Used when the host owns its own,
   *  richer resolution control (e.g. Data Lab's bar-timeframe dropdown)
   *  and only needs the picker for symbol/date/advisories. */
  readonly hideSampling = input(false);

  /**
   * `card` renders the standalone Backtest data frame. `embedded` removes
   * that duplicate frame when the host already owns the section chrome.
   */
  readonly presentation = input<TickerRangePickerPresentation>('card');

  readonly title = input('Backtest data');
  readonly sessionMode = input<SessionMode>('preview');

  protected readonly bounds = computed<DateBounds>(() => {
    const bounds = this.dateBounds();
    return bounds === 'provider' ? this.provider.bounds() : bounds;
  });

  readonly advisories = computed<readonly Advisory[]>(() => computeAdvisories(this.value()));

  /** Applies an advisory's one-click patch to ``value``. */
  onAdvisoryAction(action: AdvisoryAction): void {
    this.value.set({ ...this.value(), ...action.patch });
  }
}
