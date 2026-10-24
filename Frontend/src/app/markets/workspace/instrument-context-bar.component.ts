import { ChangeDetectionStrategy, Component, inject, input, linkedSignal } from '@angular/core';

import { BAR_TIMEFRAMES } from '../../components/data-lab/data-lab-scope';
import { DateRangePickerComponent, ProviderBoundsService, type QuickRangeKey } from '../../shared/date-picker';
import { SymbolPickerComponent } from '../../shared/symbol-picker/symbol-picker.component';
import { InstrumentContextService } from '../instrument-context.service';

/**
 * The shared pickers that commit an instrument's context (PRD #3490 R05, R09):
 * the instrument, the window with its quick ranges, the bar timeframe and the
 * session.
 *
 * There is no Apply. Each control commits what it holds the moment it holds a
 * complete, usable value, and the tabs under it read what was committed. The
 * date picker hands an empty date for a half-typed, refused or reversed entry,
 * and an empty date commits nothing: the entry stays in its field with the
 * picker's own reason beside it.
 */
@Component({
  selector: 'app-instrument-context-bar',
  imports: [SymbolPickerComponent, DateRangePickerComponent],
  templateUrl: './instrument-context-bar.component.html',
  styleUrl: './instrument-context-bar.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InstrumentContextBarComponent {
  protected readonly context = inject(InstrumentContextService);

  /**
   * Whether the window, timeframe and session are offered. The Options tab has its own context
   * (PRD #3490 R09), so over it only the instrument is: controls that do not move what is on
   * screen would say a change there reaches it.
   */
  readonly windowControls = input(true);

  protected readonly barTimeframes = BAR_TIMEFRAMES;
  /**
   * The chart charts the session still forming and takes any calendar day, so
   * its dates run from the provider's oldest day with no last day.
   */
  protected readonly dateBounds = inject(ProviderBoundsService).openEnded;

  /** The dates the picker holds. They follow what is committed, and a date the picker refuses is its own to show. */
  protected readonly fromIso = linkedSignal(() => this.context.shown().window?.from ?? '');
  protected readonly toIso = linkedSignal(() => this.context.shown().window?.to ?? '');

  protected onSymbolPicked(symbol: string): void {
    this.context.switchSymbol(symbol);
  }

  protected onDatesChanged(dates: { readonly from: string; readonly to: string }): void {
    this.context.commitWindow(dates);
  }

  protected onPresetPicked(key: QuickRangeKey): void {
    void this.context.commitPreset(key);
  }

  protected onTimeframeChange(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.context.commitTimeframe(event.target.value);
  }

  protected onSessionChange(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.context.commitSession(event.target.value);
  }
}
