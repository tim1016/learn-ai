import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';

import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import type { AlpacaOrderDraftLeg } from './alpaca-order-entry.types';

/** Presentation-only editor for one Signal Forms-backed Alpaca order leg. */
@Component({
  selector: 'app-alpaca-order-leg-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField, ButtonModule, InputTextModule, SymbolPickerComponent],
  templateUrl: './alpaca-order-leg-row.component.html',
  styleUrl: './alpaca-order-leg-row.component.scss',
  host: {
    class: 'order-leg',
    role: 'listitem',
  },
})
export class AlpacaOrderLegRowComponent {
  readonly legForm = input.required<FieldTree<AlpacaOrderDraftLeg, number>>();
  readonly position = input.required<number>();
  /** The SQLite tracer deliberately exposes only its qualified market/DAY shape. */
  readonly manualMarketOnly = input(false);
  readonly removed = output();

  /**
   * The shared picker owns symbol selection (ADR 0066): the joined catalog
   * with the ensure-coverage gate, one story everywhere including order
   * entry. The pick writes this leg's Signal Forms field; the draft leg —
   * and the preview it drives — reads the same field.
   */
  onSymbolPicked(symbol: string): void {
    this.legForm().symbol().value.set(symbol);
  }
}
