import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import type { OperatorMove } from '../../../../api/operator-blocker.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtCurrency } from '../../format';

/** One posture line: a backend headline, its detail, and its navigate move. */
export interface AccountStatusView {
  readonly headline: string;
  readonly detail: string | null;
  readonly move: OperatorMove | null;
}

export interface ChannelPosture {
  readonly name: string;
  readonly healthy: boolean;
}

/**
 * The account facts the compact triage header has no room for.
 *
 * Disclosed rather than dropped: the `fleet_roster` operator posture carries
 * the only account-recovery move this surface can dispatch, so it must stay
 * reachable. Custody alerts deliberately live outside this component — they
 * gate order-producing activity and are never hidden behind a disclosure.
 */
@Component({
  selector: 'app-account-posture-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-posture-detail.component.html',
  styleUrl: './account-posture-detail.component.scss',
})
export class AccountPostureDetailComponent {
  readonly account = input.required<BrokerAccountSnapshot>();
  readonly clerkStatus = input<ClerkStatus | null>(null);
  readonly accountStatus = input<AccountStatusView | null>(null);
  readonly channels = input<readonly ChannelPosture[]>([]);
  readonly clerkUnavailable = input(false);
  readonly refreshing = input(false);
  readonly moveRequested = output<OperatorMove>();

  protected readonly fmtCurrency = fmtCurrency;
}
