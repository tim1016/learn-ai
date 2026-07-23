import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type { BrokerOrder } from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BrokersService } from '../../../services/brokers.service';

// Statuses for which Alpaca accepts a cancel — an order still working (resting
// or partially filled). Terminal statuses (filled/canceled/expired/rejected)
// are not cancelable, so the action is hidden for them rather than offered and
// failing. Mirrors the Alpaca order-status lifecycle.
const CANCELABLE_STATUSES: ReadonlySet<string> = new Set([
  'new',
  'accepted',
  'pending_new',
  'partially_filled',
  'held',
  'accepted_for_bidding',
]);

/**
 * Alpaca recent-orders table. Shows what any channel submitted to the account,
 * and (phase-2 S3) offers a Cancel action on each still-working row. Four
 * distinct renders: loading, error, honest-empty (no recent orders), and the
 * table.
 */
@Component({
  selector: 'app-alpaca-orders-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DecimalPipe,
    ReceiptLabelPipe,
    ButtonModule,
    TableModule,
    TagModule,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-orders-table.component.html',
  host: { class: 'block' },
})
export class AlpacaOrdersTableComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly orders = resource({
    loader: () => this.brokers.listOrders('alpaca', { status: 'all', limit: 50 }),
  });

  // The order_id currently being canceled (disables its button + shows a
  // spinner), or null when idle. One cancel at a time keeps the UI honest.
  protected readonly cancelingId = signal<string | null>(null);
  // A per-order cancel failure message, keyed by order_id, cleared on retry.
  protected readonly cancelError = signal<Record<string, string>>({});

  protected isCancelable(order: BrokerOrder): boolean {
    return CANCELABLE_STATUSES.has(order.status);
  }

  protected async cancel(order: BrokerOrder): Promise<void> {
    if (this.cancelingId() !== null) return;
    this.cancelingId.set(order.order_id);
    this.clearError(order.order_id);
    try {
      const result = await this.brokers.cancelOrder('alpaca', order.order_id);
      if (result.status === 'failed') {
        this.setError(
          order.order_id,
          result.error?.message ?? 'Alpaca could not cancel this order.',
        );
      } else {
        // Successful cancel — refresh so the row reflects its new status.
        this.orders.reload();
      }
    } catch {
      this.setError(order.order_id, 'Could not reach Alpaca to cancel this order.');
    } finally {
      this.cancelingId.set(null);
    }
  }

  private setError(orderId: string, message: string): void {
    this.cancelError.update((errors) => ({ ...errors, [orderId]: message }));
  }

  private clearError(orderId: string): void {
    this.cancelError.update((errors) => {
      const { [orderId]: _removed, ...rest } = errors;
      return rest;
    });
  }
}
