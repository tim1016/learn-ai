import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LifecycleChartReceipt } from '../../../api/live-instances.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { fmtTimestampNy } from '../format';

@Component({
  selector: 'app-node-receipts-pane',
  imports: [CommonModule, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './node-receipts-pane.component.html',
  styleUrl: './node-receipts-pane.component.scss',
})
export class NodeReceiptsPaneComponent {
  readonly receipts = input<LifecycleChartReceipt[]>([]);

  receiptTimestamp(receipt: LifecycleChartReceipt): string | null {
    if (receipt.ts_ms === null) return null;
    return receipt.ts_ms_resolved ? fmtTimestampNy(receipt.ts_ms) : 'timestamp unresolved';
  }

  trackNodeReceipt(index: number, receipt: LifecycleChartReceipt): string {
    return `${receipt.label}:${receipt.source ?? 'unknown'}:${index}`;
  }
}
