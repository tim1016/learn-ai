import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LifecycleChartReceipt } from '../../../api/live-instances.types';
import {
  formatReceiptLabel,
  formatReceiptValue,
} from '../../../shared/pipes/receipt-label.pipe';
import { fmtTimestampNy } from '../format';

@Component({
  selector: 'app-node-receipts-list',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './node-receipts-list.component.html',
  styleUrl: './node-receipts-list.component.scss',
})
export class NodeReceiptsListComponent {
  readonly receipts = input<LifecycleChartReceipt[]>([]);

  receiptHeadline(receipt: LifecycleChartReceipt): string {
    return receipt.headline?.trim() || this.receiptLine(receipt);
  }

  receiptLine(receipt: LifecycleChartReceipt): string {
    return `${formatReceiptLabel(receipt.label)} is ${formatReceiptValue(receipt.label, receipt.value)}${receipt.unit ? ` ${receipt.unit}` : ''}.`;
  }

  receiptDetail(receipt: LifecycleChartReceipt): string | null {
    const parts = [
      receipt.source ? `Source: ${formatReceiptLabel(receipt.source)}` : null,
      receipt.gate_id ? `Gate: ${formatReceiptLabel(receipt.gate_id)}` : null,
      receipt.ts_ms_resolved && receipt.ts_ms !== null ? `Evidence time: ${fmtTimestampNy(receipt.ts_ms)}` : null,
    ];
    const detail = parts.filter((part): part is string => part !== null).join('. ');
    return detail || null;
  }

  trackNodeReceipt(index: number, receipt: LifecycleChartReceipt): string {
    return `${receipt.label}:${receipt.source ?? 'unknown'}:${index}`;
  }
}
