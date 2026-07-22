import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BrokersService } from '../../../services/brokers.service';

/**
 * Alpaca recent-orders table. Read-only — shows what any channel submitted to
 * the account. Four distinct renders: loading, error, honest-empty (no recent
 * orders), and the table.
 */
@Component({
  selector: 'app-alpaca-orders-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-orders-table.component.html',
  styleUrl: './alpaca-orders-table.component.scss',
})
export class AlpacaOrdersTableComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly orders = resource({
    loader: () => this.brokers.listOrders('alpaca', { status: 'all', limit: 50 }),
  });
}
