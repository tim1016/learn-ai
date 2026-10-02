import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { FILL_MODE_OPTIONS, isFillModeName } from '../../models/fill-mode';
import { numberProblemKey, type PlanEdit, type ProtocolNumberField } from './golden-search-plan-draft';
import { FILL_MODE_INPUT_ID, numberInputId } from './golden-search-plan-problems';
import type { ProtocolRequest } from './golden-search.types';

/**
 * Capital, fills and trading costs (#2696), folded away by default under a
 * one-line summary: flat costs every evaluation pays, and the cost stresses
 * the evidence adds on top. Not a historical broker fee schedule.
 */
@Component({
  selector: 'app-golden-search-cost-controls',
  imports: [CurrencyPipe, InputText],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-cost-controls.component.html',
  styleUrl: './golden-search-plan-card.scss',
})
export class GoldenSearchCostControlsComponent {
  readonly protocol = input.required<ProtocolRequest>();
  readonly problems = input<ReadonlyMap<string, string>>(new Map());
  readonly edit = output<PlanEdit>();

  protected readonly fillModes = FILL_MODE_OPTIONS;
  protected readonly inputId = numberInputId;
  protected readonly fillModeId = FILL_MODE_INPUT_ID;
  protected readonly fillModeLabel = computed(() => FILL_MODE_OPTIONS.find((option) => option.value === this.protocol().execution.fill_mode)?.label ?? this.protocol().execution.fill_mode);

  protected numberInvalid(field: ProtocolNumberField): boolean {
    return this.problems().has(numberProblemKey(field));
  }

  protected onNumber(field: ProtocolNumberField, raw: string): void {
    this.edit.emit({ kind: 'number', field, raw });
  }

  protected onFillMode(event: Event): void {
    const raw = event.target instanceof HTMLSelectElement ? event.target.value : null;
    if (isFillModeName(raw)) this.edit.emit({ kind: 'fill-mode', fillMode: raw });
  }
}
