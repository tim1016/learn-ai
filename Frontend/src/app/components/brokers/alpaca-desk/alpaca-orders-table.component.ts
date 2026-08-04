import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { Popover, PopoverModule } from 'primeng/popover';
import { Table, TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type { BrokerOrder } from '../../../api/alpaca.types';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BrokersService } from '../../../services/brokers.service';
import { orderStatusSeverity, OrderReferenceCardComponent } from './order-reference-card.component';

// Hide Cancel only for terminal states and states where Alpaca itself rejects a
// second cancel. This intentionally leaves `done_for_day` GTC orders eligible:
// they resume on the next session and still need an operator escape hatch.
const NON_CANCELABLE_STATUSES: ReadonlySet<string> = new Set([
  'filled',
  'canceled',
  'expired',
  'rejected',
  'replaced',
  'pending_cancel',
  'pending_replace',
]);

/**
 * Searchable Alpaca transaction history. Rows preserve the broker's newest-
 * first order while PrimeNG provides operator sorting, filtering, and paging.
 * Still-working orders retain the phase-2 S3 Cancel action.
 */
@Component({
  selector: 'app-alpaca-orders-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DecimalPipe,
    AssetIdentityComponent,
    ReceiptLabelPipe,
    ButtonModule,
    PopoverModule,
    TableModule,
    TagModule,
    TimestampDisplayComponent,
    OrderReferenceCardComponent,
  ],
  templateUrl: './alpaca-orders-table.component.html',
  styleUrl: './alpaca-orders-table.component.scss',
  host: { class: 'block' },
})
export class AlpacaOrdersTableComponent {
  private readonly brokers = inject(BrokersService);
  readonly refreshVersion = input(0);

  protected readonly orders = resource({
    params: () => this.refreshVersion(),
    loader: () => this.brokers.listOrders('alpaca', { status: 'all', limit: 50 }),
  });

  // The order_id currently being canceled (disables its button + shows a
  // spinner), or null when idle. One cancel at a time keeps the UI honest.
  protected readonly cancelingId = signal<string | null>(null);
  // A per-order cancel failure message, keyed by order_id, cleared on retry.
  protected readonly cancelError = signal<Record<string, string>>({});
  protected readonly selectedOrder = signal<BrokerOrder | null>(null);
  protected readonly searchQuery = signal('');
  protected readonly orderTypeFilter = signal('');
  protected readonly statusFilter = signal('');
  // Derived from live data, not a hardcoded allowlist: order_type is an
  // unconstrained broker-reported string, so a stop/stop_limit/trailing_stop
  // order placed through another channel must still be filterable.
  protected readonly orderTypes = computed(() =>
    [...new Set((this.orders.value() ?? []).map((order) => order.order_type))].sort(),
  );
  protected readonly statusOptions = computed(() =>
    [...new Set((this.orders.value() ?? []).map((order) => order.status))].sort(),
  );

  protected inputValue(event: Event): string {
    const target = event.target;
    return target instanceof HTMLInputElement || target instanceof HTMLSelectElement
      ? target.value
      : '';
  }

  protected search(table: Table, event: Event): void {
    const value = this.inputValue(event);
    this.searchQuery.set(value);
    table.filterGlobal(value, 'contains');
  }

  protected filterExact(table: Table, field: string, event: Event): void {
    const value = this.inputValue(event);
    if (field === 'order_type') this.orderTypeFilter.set(value);
    if (field === 'status') this.statusFilter.set(value);
    table.filter(value || null, field, 'equals');
  }

  protected clearFilters(table: Table): void {
    this.searchQuery.set('');
    this.orderTypeFilter.set('');
    this.statusFilter.set('');
    table.clear();
  }

  protected showDetails(order: BrokerOrder, event: Event, popover: Popover): void {
    this.selectedOrder.set(order);
    popover.toggle(event);
  }

  protected isCancelable(order: BrokerOrder): boolean {
    return !NON_CANCELABLE_STATUSES.has(order.status);
  }

  protected readonly statusSeverity = orderStatusSeverity;

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
