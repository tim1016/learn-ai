import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { TableModule } from 'primeng/table';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { AlpacaOrderDraftLeg } from './alpaca-order-entry.types';

/** Presentation-only confirmation dialog for the current Alpaca order draft. */
@Component({
  selector: 'app-alpaca-order-preview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ButtonModule, DialogModule, TableModule, ReceiptLabelPipe],
  templateUrl: './alpaca-order-preview.component.html',
})
export class AlpacaOrderPreviewComponent {
  readonly legs = input.required<AlpacaOrderDraftLeg[]>();
  readonly visible = input(false);
  readonly submitting = input(false);
  readonly closed = output();
  readonly confirmed = output();
}
