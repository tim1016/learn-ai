import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { LinkState } from '../../../services/broker-connectivity.service';

export interface OrderFeedStatus {
  broker: {
    state: LinkState;
    headline: string;
    detail: string;
  };
  updates: {
    state: LinkState;
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
