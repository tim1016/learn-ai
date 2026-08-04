import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { TagModule, type TagSeverity } from 'primeng/tag';

import type { BrokerOrder } from '../../../api/alpaca.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';

export function orderStatusSeverity(status: string): TagSeverity {
  if (status === 'filled') return 'success';
  if (status === 'rejected') return 'danger';
  if (status === 'canceled' || status === 'expired' || status === 'replaced') {
    return 'secondary';
  }
  if (status === 'pending_cancel' || status === 'pending_replace') return 'warn';
  return 'info';
}

/**
 * Broker-verifiable order reference card shown inside the transaction
 * table's info popover. Presentational only — the table owns popover
 * open/close and which order is selected.
 */
@Component({
  selector: 'app-order-reference-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe, AssetIdentityComponent, ReceiptLabelPipe, TagModule, TimestampDisplayComponent],
  templateUrl: './order-reference-card.component.html',
  styleUrl: './order-reference-card.component.scss',
  host: { class: 'contents' },
})
export class OrderReferenceCardComponent {
  readonly order = input.required<BrokerOrder>();
  protected readonly statusSeverity = orderStatusSeverity;
}
