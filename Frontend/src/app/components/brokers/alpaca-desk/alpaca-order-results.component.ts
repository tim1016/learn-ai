import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type { OrderLegResult } from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';

/** Presentation-only table for typed per-leg Alpaca submission outcomes. */
@Component({
  selector: 'app-alpaca-order-results',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TableModule, TagModule, ReceiptLabelPipe],
  templateUrl: './alpaca-order-results.component.html',
})
export class AlpacaOrderResultsComponent {
  readonly results = input<OrderLegResult[] | null>(null);
}
