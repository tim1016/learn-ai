import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  resource,
  signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { TagModule } from 'primeng/tag';

import { AlpacaAccountCardComponent } from './alpaca-account-card.component';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';
import { AlpacaHoldBannerComponent } from './alpaca-hold-banner.component';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';
import { AlpacaOrdersTableComponent } from './alpaca-orders-table.component';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';
import { BrokersService } from '../../../services/brokers.service';
import { parseManualOrderTicketQuery } from '../../broker/lib/manual-order-navigation';

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
    AlpacaCustodyResolutionComponent,
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
  private readonly route = inject(ActivatedRoute);
  private readonly brokers = inject(BrokersService);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  private readonly routedOrderPrefill = computed(() =>
    parseManualOrderTicketQuery(this.queryParams()),
  );
  private readonly account = resource({
    loader: () => this.brokers.getAccount('alpaca'),
  });

  protected readonly ticketAccountId = computed(() =>
    this.account.hasValue() ? this.account.value().account_id : null,
  );
  protected readonly orderPrefill = computed(() => {
    const routed = this.routedOrderPrefill();
    const accountId = this.ticketAccountId();
    return routed !== null && accountId === routed.accountId ? routed : null;
  });
  protected readonly orderRouteMismatch = computed(() => {
    const routed = this.routedOrderPrefill();
    const accountId = this.ticketAccountId();
    return routed !== null && accountId !== null && routed.accountId !== accountId
      ? `The order link targets account ${routed.accountId}, but Alpaca is connected to ${accountId}. No ticket was opened.`
      : null;
  });
  protected readonly orderEntryOpen = signal(false);
  protected readonly ordersRefreshVersion = signal(0);

  constructor() {
    effect(() => {
      if (this.orderPrefill() !== null) this.orderEntryOpen.set(true);
    });
  }

  protected refreshOrders(): void {
    this.ordersRefreshVersion.update((version) => version + 1);
  }
}
