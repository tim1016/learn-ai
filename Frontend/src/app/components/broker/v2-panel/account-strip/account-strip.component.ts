import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import type { AccountOperatorPosture } from '../../../../api/operator-blocker.types';
import { accountOperatorPostureBlocker } from '../../../../api/operator-blocker.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtCurrency } from '../../format';

interface ChannelPosture {
  readonly name: string;
  readonly healthy: boolean;
}

interface AccountStatusView {
  readonly headline: string;
  readonly detail: string | null;
}

/**
 * Pure account-level posture for the bots fleet container. The single
 * "Account status" fact renders only the backend-authored `fleet_roster`
 * projection of `ClerkStatus.operator_posture` (issue #1664) — it never
 * combines `trading_blocked`/`account_blocked`/`freeze`/`hold` into a
 * client-derived verdict.
 */
@Component({
  selector: 'app-account-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './account-strip.component.html',
  styleUrl: './account-strip.component.scss',
  host: { class: 'block' },
})
export class AccountStripComponent {
  readonly account = input<BrokerAccountSnapshot | null>(null);
  readonly clerkStatus = input<ClerkStatus | null>(null);
  readonly loading = input(false);
  readonly refreshing = input(false);
  readonly accountUnavailable = input(false);
  readonly clerkUnavailable = input(false);

  protected readonly fmtCurrency = fmtCurrency;

  protected readonly channels = computed<readonly ChannelPosture[]>(() =>
    (this.clerkStatus()?.channel_healths ?? []).map((channel) => ({
      name: channel.stream,
      healthy: channel.healthy,
    })),
  );

  protected readonly reconciliation = computed(
    () => this.clerkStatus()?.latest_reconciliation ?? null,
  );

  protected readonly holdActive = computed(
    () => this.clerkStatus()?.hold?.active ?? false,
  );

  protected readonly freezeActive = computed(
    () => this.clerkStatus()?.freeze?.active ?? false,
  );

  private readonly posture = computed<AccountOperatorPosture | null>(
    () => this.clerkStatus()?.operator_posture ?? null,
  );

  /** Fails closed (null) when the posture hasn't loaded yet — never re-derived from raw facts. */
  protected readonly accountStatus = computed<AccountStatusView | null>(() => {
    const posture = this.posture();
    if (posture === null) return null;
    const blocker = accountOperatorPostureBlocker(posture, 'fleet_roster');
    return blocker !== null
      ? { headline: blocker.headline, detail: blocker.detail ?? null }
      : { headline: posture.status_headline, detail: posture.status_detail };
  });
}
