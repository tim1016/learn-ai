import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

export interface ActionReceiptView {
  readonly actionId: string;
  readonly outcome: 'success' | 'conflict' | 'failure' | 'unknown';
  readonly receiptId: string | null;
  readonly recordedAtMs: number;
  readonly message: string;
}

@Component({
  selector: 'app-panel-action-receipt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './panel-action-receipt.component.html',
  styleUrl: './panel-action-receipt.component.scss',
})
export class PanelActionReceiptComponent {
  readonly receipt = input.required<ActionReceiptView>();
  readonly dismissed = output();
}
