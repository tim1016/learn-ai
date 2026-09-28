import { ChangeDetectionStrategy, Component, computed, effect, inject, resource, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute } from '@angular/router';
import { DialogModule } from 'primeng/dialog';

import { parseManualOrderTicketQuery } from '../../broker/lib/manual-order-navigation';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';

/**
 * The manual-order ticket, opened on an account's Home by a link carrying a
 * ticket in its query (`buildManualOrderTicketNavigation` — a bot page's
 * "Manual order"). It opens only for the account the header shows and only
 * once the server's manual-order capability has been read; a link naming
 * another account opens nothing and says so.
 *
 * Moved unchanged from the retired Overview page, which hosted it before Home
 * replaced it (PRD #2560).
 */
@Component({
  selector: 'app-alpaca-manual-order-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaOrderEntryComponent, DialogModule],
  templateUrl: './alpaca-manual-order-host.component.html',
  styles: [`
    :host { display: block; }
    .order-route-notice {
      margin: 0;
      padding: 0.75rem 1rem;
      border: 1px solid var(--warning-border, var(--border-light));
      border-radius: var(--radius);
      background: var(--warning-soft, var(--bg-surface));
      color: var(--text-primary);
    }
  `],
})
export class AlpacaManualOrderHostComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly brokers = inject(BrokersService);
  private readonly accountData = inject(AlpacaDeskAccountDataService);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly target = this.accountData.target;
  /** The frozen command fence order entry must mint against — never the live
   * directory (#2106). See `AlpacaDeskAccountDataService.fence`. */
  protected readonly fence = this.accountData.fence;

  private readonly routedOrderPrefill = computed(() =>
    parseManualOrderTicketQuery(this.queryParams()),
  );
  protected readonly ticketAccountId = computed(() =>
    this.accountData.account.hasValue() ? this.accountData.account.value().account_id : null,
  );
  protected readonly orderPrefill = computed(() => {
    const routed = this.routedOrderPrefill();
    return routed !== null && routed.accountId === this.ticketAccountId() ? routed : null;
  });
  protected readonly orderRouteMismatch = computed(() => {
    const routed = this.routedOrderPrefill();
    const accountId = this.ticketAccountId();
    return routed !== null && accountId !== null && routed.accountId !== accountId
      ? `The order link targets account ${routed.accountId}, but Alpaca is connected to ${accountId}. No ticket was opened.`
      : null;
  });
  private readonly manualOrderCapability = resource({
    params: () => {
      const accountId = this.orderPrefill()?.accountId;
      const target = this.target();
      return accountId === undefined || target === null ? undefined : { target, accountId };
    },
    loader: ({ params }) =>
      this.brokers.getSqliteManualOrderCapability(params.target.clerkId, params.accountId),
  });
  protected readonly manualTicketCapability = computed(
    () => this.manualOrderCapability.hasValue()
      ? this.manualOrderCapability.value()
      : null,
  );
  protected readonly manualOrderNotice = computed(() => {
    if (this.orderPrefill() === null) return null;
    if (this.manualOrderCapability.isLoading()) {
      return 'Checking whether manual orders are allowed before opening this ticket.';
    }
    if (this.manualOrderCapability.error() !== undefined) {
      return 'Whether manual orders are allowed could not be read. No ticket was opened.';
    }
    return null;
  });
  protected readonly orderEntryOpen = signal(false);

  constructor() {
    effect(() => {
      this.orderEntryOpen.set(
        this.orderPrefill() !== null && this.manualOrderCapability.hasValue(),
      );
    });
  }
}
