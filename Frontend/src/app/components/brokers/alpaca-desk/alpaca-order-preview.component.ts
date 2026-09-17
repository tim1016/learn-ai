import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { TableModule } from 'primeng/table';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { AlpacaOrderDraftLeg } from './alpaca-order-entry.types';

/** Presentation-only confirmation dialog for the current Alpaca order draft.
 *
 * Names the account the draft will be submitted against by its *number*
 * (ADR 0064; #2188). Submitting a manual order is a consequential action, and
 * a confirmation is one of the two places the number earns its place — the
 * other being Configuration, whose `Account <strong>…</strong>` idiom this
 * mirrors. Everywhere else an account is shown by name. */
@Component({
  selector: 'app-alpaca-order-preview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, DialogModule, TableModule, ReceiptLabelPipe],
  templateUrl: './alpaca-order-preview.component.html',
})
export class AlpacaOrderPreviewComponent {
  readonly legs = input.required<AlpacaOrderDraftLeg[]>();
  /** The account these legs are submitted against — the one fact this dialog
   * exists to have the operator confirm alongside the legs themselves. */
  readonly accountId = input.required<string>();
  readonly visible = input(false);
  readonly submitting = input(false);
  readonly closed = output();
  readonly confirmed = output();
}
