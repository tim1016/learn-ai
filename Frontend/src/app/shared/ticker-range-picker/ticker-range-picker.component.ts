import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
  output,
} from '@angular/core';
import { CommonModule } from '@angular/common';

import {
  computeAdvisories,
  daysBetween,
  summarizeAvailability,
  weekdaysBetween,
  type Advisory,
  type AdvisoryAction,
  type AvailabilityCell,
  type Resolution,
  type TickerOption,
  type TickerRange,
} from './ticker-range-picker.types';
import { InstrumentCardComponent } from './parts/instrument-card.component';
import {
  TimeWindowCardComponent,
  type LegendTreatment,
} from './parts/time-window-card.component';
import {
  SamplingCardComponent,
  type SessionMode,
} from './parts/sampling-card.component';

export type { LegendTreatment, SessionMode };

/**
 * Shared ticker + range picker.
 *
 * Composes three child components under ``parts/``:
 *   <app-instrument-card>   — symbol combobox + dropdown + cache hint
 *   <app-time-window-card>  — date inputs + presets + availability strip + legend
 *   <app-sampling-card>     — resolution toggle + opt-in multiplier + session + auto-fetch
 *
 * Owns the cross-card concerns: the outer card frame, the summary
 * header, and the advisory cluster. Sub-component state is encapsulated
 * (open/query for the dropdown, presets list, etc.). Both two-way
 * bindings flow through the same ``value`` model.
 */
@Component({
  selector: 'app-ticker-range-picker',
  imports: [
    CommonModule,
    InstrumentCardComponent,
    TimeWindowCardComponent,
    SamplingCardComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './ticker-range-picker.component.html',
  styleUrls: ['./ticker-range-picker.component.scss'],
})
export class TickerRangePickerComponent {
  readonly value = model.required<TickerRange>();

  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recent = input<readonly string[]>([]);
  readonly availability = input<readonly AvailabilityCell[]>([]);

  readonly availableResolutions = input<readonly Resolution[]>([
    'minute',
    'hour',
    'daily',
  ]);
  readonly availableMultipliers = input<readonly number[]>([]);

  readonly showAutoFetch = input(true);

  /** Hides the entire Sampling card. Used when the host owns its own,
   *  richer resolution control (e.g. Data Lab's bar-timeframe dropdown)
   *  and only needs the picker for symbol/date/availability/advisories. */
  readonly hideSampling = input(false);

  /** Deprecated alias for ``hideSampling``. Kept for one PR cycle so
   *  any out-of-tree caller setting ``hideResolution=true`` keeps
   *  working. Removed in PR (iii)'s Task 10b. */
  readonly hideResolution = input(false);

  readonly title = input('Backtest data');
  readonly legendTreatment = input<LegendTreatment>('tinted-bold');
  readonly sessionMode = input<SessionMode>('preview');

  readonly advisoryAction = output<AdvisoryAction>();

  /** Sampling card is hidden when EITHER input is true (deprecation
   *  bridge — drops to plain ``hideSampling()`` in PR (iii)). */
  protected readonly samplingHidden = computed(
    () => this.hideSampling() || this.hideResolution(),
  );

  // Advisories are computed at this layer — they read the union of
  // (range state, availability summary), neither of which is owned by
  // a single sub-component. The TimeWindowCard recomputes its own
  // summary independently for the legend display; this is intentional
  // duplication that keeps each component self-contained.
  readonly summary = computed(() => summarizeAvailability(this.availability()));
  readonly advisories = computed<readonly Advisory[]>(() =>
    computeAdvisories(this.value(), this.summary()),
  );

  // Kept as readonly accessors for any host that reads them directly
  // (e.g. tests verifying the picker's range arithmetic). The values
  // are also rendered by the TimeWindowCard's own internal computeds
  // for header display.
  readonly spanDays = computed(() => {
    const v = this.value();
    return daysBetween(v.from, v.to);
  });
  readonly spanBusinessDays = computed(() => {
    const summaryDays = this.summary().weekdays;
    if (summaryDays > 0) return summaryDays;
    const v = this.value();
    return weekdaysBetween(v.from, v.to);
  });

  /**
   * Click handler for an advisory's action button. Applies the patch
   * (if any) to ``value`` and re-emits the action so the host can honor
   * side-effect flags like ``triggerRun``.
   */
  onAdvisoryAction(action: AdvisoryAction): void {
    if (action.patch) {
      this.value.set({ ...this.value(), ...action.patch });
    }
    this.advisoryAction.emit(action);
  }
}
