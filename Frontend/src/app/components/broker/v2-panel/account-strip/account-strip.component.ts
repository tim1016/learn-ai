import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtCurrency } from '../../format';

interface ChannelPosture {
  readonly name: string;
  readonly healthy: boolean;
}

/** Pure account-level posture for the bots fleet container. */
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
}
