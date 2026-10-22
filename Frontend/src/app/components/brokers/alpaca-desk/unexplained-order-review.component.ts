import { ChangeDetectionStrategy, Component, computed, inject, input, output, resource } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokerOrderReviewComponent, type BrokerOrderAcknowledgementRequest } from './broker-order-review.component';

/** Reads the exact outside order named by an account hold before offering review (#3468). */
@Component({
  selector: 'app-unexplained-order-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent, BrokerOrderReviewComponent],
  templateUrl: './unexplained-order-review.component.html',
  styleUrl: './unexplained-order-review.component.scss',
})
export class UnexplainedOrderReviewComponent {
  readonly accountId = input.required<string>();
  readonly target = input.required<ResourceTarget>();
  readonly brokerOrderId = input.required<string>();
  readonly busyOrderId = input<string | null>(null);
  readonly acknowledge = output<BrokerOrderAcknowledgementRequest>();
  private readonly brokers = inject(BrokersService);

  protected readonly order = resource({
    params: () => ({ target: this.target(), accountId: this.accountId(), orderId: this.brokerOrderId() }),
    loader: async ({ params }) => {
      const detail = await this.brokers.accountTransaction(
        params.target.clerkId, params.accountId, `external-order:${params.orderId}`,
      );
      if (detail.account_id !== params.accountId || detail.external_order_id !== params.orderId
        || detail.transaction_origin !== 'external'
        || typeof detail.receipt?.['latest_observation_sequence'] !== 'number') {
        throw new Error('The recorded evidence does not identify this outside order.');
      }
      return detail;
    },
  });
  protected acknowledgeObserved(request: BrokerOrderAcknowledgementRequest): void {
    if (!this.order.hasValue()) return;
    const sequence = this.order.value().receipt?.['latest_observation_sequence'];
    if (typeof sequence === 'number') this.acknowledge.emit({ ...request, expectedObservationSequence: sequence });
  }

  protected readonly brokerState = computed(() => {
    if (!this.order.hasValue()) return null;
    const state = this.order.value().receipt?.['broker_state'];
    return typeof state === 'string' ? state : null;
  });
}
