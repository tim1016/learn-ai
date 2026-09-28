import {
  ChangeDetectionStrategy,
  Component,
  input,
} from '@angular/core';

import type { ChartFillMarker } from '../../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../../shared/timestamp/timestamp-display.component';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { fmtCurrency } from '../../../format';

/** Shared row presentation for the inline fills rail and the full-history drawer. */
@Component({
  selector: 'app-fill-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './fill-table.component.html',
  styleUrl: './fill-table.component.scss',
})
export class FillTableComponent {
  readonly fills = input<readonly ChartFillMarker[]>([]);
  readonly label = input.required<string>();

  protected readonly fmtCurrency = fmtCurrency;
}
