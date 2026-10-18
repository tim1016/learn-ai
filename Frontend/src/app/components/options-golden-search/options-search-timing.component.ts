import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal } from '@angular/core';
import { form, FormField } from '@angular/forms/signals';

import type { ServedSetup } from '../options-backtest/options-backtest.types';
import { distanceInWords, MS_PER_MINUTE } from '../options-backtest/session-time';
import type { KnobSelection } from './options-search-knobs.component';

@Component({
  selector: 'app-options-search-timing',
  imports: [FormField],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-search-timing.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsSearchTimingComponent {
  readonly setup = input.required<ServedSetup>();
  readonly changed = output<readonly KnobSelection[] | null>();
  protected readonly model = signal({ entry: { min: 0, max: 0, step: 1 }, exit: { min: 0, max: 0, step: 1 } });
  protected readonly fields = form(this.model);
  protected readonly roles = ['entry', 'exit'] as const;
  protected readonly description = distanceInWords;
  protected readonly selection = computed(() => {
    const selections: KnobSelection[] = [];
    for (const role of this.roles) {
      const range = this.model()[role];
      const saved = this.setup()[role];
      if (![range.min, range.max, range.step].every(Number.isSafeInteger) || range.step <= 0 || range.min > range.max) return null;
      if (range.min === 0 && range.max === 0) continue;
      if (range.min === range.max || (range.max - range.min) % range.step !== 0 || (range.max - range.min) / range.step >= 5000) return null;
      const values: number[] = [];
      for (let shift = range.min; shift <= range.max; shift += range.step) {
        const offset = saved.offset_ms + (saved.anchor === 'open' ? shift : -shift) * MS_PER_MINUTE;
        if (offset < 0 || offset > 86_340_000) return null;
        values.push(offset);
      }
      selections.push({ path: `${role}.offset_ms`, values, priority: 5, mode: 'search' });
    }
    return selections;
  });

  constructor() {
    effect(() => { this.setup(); this.model.set({ entry: { min: 0, max: 0, step: 1 }, exit: { min: 0, max: 0, step: 1 } }); });
    effect(() => this.changed.emit(this.selection()));
  }
}
