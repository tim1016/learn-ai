import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LinkState } from '../../../services/broker-connectivity.service';
import type { ClerkTransactionHistoryResponse } from '../../../api/clerk-transaction-history.types';

export type TransactionFeedState = ClerkTransactionHistoryResponse['feed_state'] | 'loading';

export interface OrderFeedStatus {
  broker: {
    state: LinkState;
    headline: string;
    detail: string;
  };
  updates: {
    state: TransactionFeedState;
    headline: string;
    detail: string;
  };
}

/** Presentational status strip for the order workspace's two live contracts. */
@Component({
  selector: 'app-broker-order-feed-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-order-feed-status.component.html',
  styleUrl: './broker-order-feed-status.component.scss',
})
export class BrokerOrderFeedStatusComponent {
  readonly status = input.required<OrderFeedStatus>();
}
