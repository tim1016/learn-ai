import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';

import type { AlpacaOrderDraftLeg } from './alpaca-order-entry.types';

/** Presentation-only editor for one Signal Forms-backed Alpaca order leg. */
@Component({
  selector: 'app-alpaca-order-leg-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField, ButtonModule, InputTextModule],
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
  readonly removed = output();
}
