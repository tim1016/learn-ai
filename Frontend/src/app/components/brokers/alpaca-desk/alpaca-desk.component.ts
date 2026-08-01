import { ChangeDetectionStrategy, Component, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { TagModule } from 'primeng/tag';

import { AlpacaAccountCardComponent } from './alpaca-account-card.component';
import { AlpacaHoldBannerComponent } from './alpaca-hold-banner.component';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';
import { AlpacaOrdersTableComponent } from './alpaca-orders-table.component';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';

/**
 * Alpaca broker desk (Broker System v2) — the `/brokers/alpaca` route target.
 * Composes the account card, positions/orders tables, and (phase-2 S1) the
 * order-entry panel. Separate from every v1 broker page.
 */
@Component({
  selector: 'app-alpaca-desk',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaAccountCardComponent,
    AlpacaHoldBannerComponent,
    AlpacaPositionsTableComponent,
    AlpacaOrdersTableComponent,
    AlpacaOrderEntryComponent,
    ButtonModule,
    DialogModule,
    TagModule,
  ],
  templateUrl: './alpaca-desk.component.html',
  styleUrl: './alpaca-desk.component.scss',
  host: { class: 'block h-full' },
})
export class AlpacaDeskComponent {
  protected readonly orderEntryOpen = signal(false);
  protected readonly ordersRefreshVersion = signal(0);

  protected refreshOrders(): void {
    this.ordersRefreshVersion.update((version) => version + 1);
  }
}
