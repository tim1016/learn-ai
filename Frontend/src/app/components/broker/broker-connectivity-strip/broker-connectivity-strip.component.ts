import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { BrokerConnectivityService } from '../../../services/broker-connectivity.service';

/**
 * Shared connectivity strip (handoff: cross-cuts broker pages). Renders the
 * three plumbing signals — host daemon, broker, fleet policy — that previously
 * collapsed into one fuzzy "unreachable/empty" state, so an operator can tell
 * daemon-down from broker-down from policy-block at a glance. Per-control
 * disable-with-reason reads the same `BrokerConnectivityService`.
 */
@Component({
  selector: 'app-broker-connectivity-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-connectivity-strip.component.html',
  styleUrl: './broker-connectivity-strip.component.scss',
})
export class BrokerConnectivityStripComponent {
  protected readonly connectivity = inject(BrokerConnectivityService);
}
