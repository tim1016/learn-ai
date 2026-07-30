import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
  resource,
} from '@angular/core';
import { TagModule } from 'primeng/tag';

import type { BrokerAccountSnapshot } from '../../../../api/alpaca.types';
import { BrokersService } from '../../../../services/brokers.service';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { fmtCurrency } from '../../format';
import { ChannelHealthDotComponent } from '../channel-health-dot/channel-health-dot.component';
import type { ChannelState } from '../lib/broker-v2-panel.types';

/**
 * Account-level strip: equity · cash · buying power · PAPER badge ·
 * reconciliation chip · hold banner · channel health dots.
 * Polls account + clerk status every 15s.
 */
@Component({
  selector: 'app-account-strip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TagModule, ReceiptLabelPipe, ChannelHealthDotComponent],
  templateUrl: './account-strip.component.html',
  styleUrl: './account-strip.component.scss',
  host: { class: 'block' },
})
export class AccountStripComponent {
  readonly broker = input.required<string>();

  private readonly brokers = inject(BrokersService);
  private readonly destroyRef = inject(DestroyRef);

  protected readonly account = resource({
    params: () => ({ broker: this.broker() }),
    loader: ({ params }: { params: { broker: string } }) =>
      this.brokers.getAccount(params.broker),
  });

  protected readonly clerkStatus = resource({
    params: () => ({ broker: this.broker() }),
    loader: ({ params }: { params: { broker: string } }) =>
      this.brokers.getClerkStatus(params.broker),
  });

  constructor() {
    // Poll via reload(): keeps the previous value while refreshing and
    // no-ops when a load is already in flight (see bots-list-page).
    const timer = setInterval(() => {
      this.account.reload();
      this.clerkStatus.reload();
    }, 15_000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  protected fmtCurrency = fmtCurrency;

  protected get reconciliationLabel(): string {
    const v = this.clerkStatus.value()?.latest_reconciliation?.verdict;
    return v ?? '';
  }

  protected get reconciliationSeverity(): 'success' | 'warn' | 'danger' | 'secondary' {
    const v = this.clerkStatus.value()?.latest_reconciliation?.verdict;
    if (v === 'clean') return 'success';
    if (v === 'stale') return 'secondary';
    return 'danger';
  }

  protected isPaper(snap: BrokerAccountSnapshot): boolean {
    // Alpaca paper accounts have IDs starting with "PA"
    return snap.account_id.startsWith('PA');
  }

  protected get holdActive(): boolean {
    return this.clerkStatus.value()?.hold?.active ?? false;
  }

  protected get holdReason(): string {
    return this.clerkStatus.value()?.hold?.reason_code ?? '';
  }

  protected get channels(): { name: string; state: ChannelState }[] {
    const ch = this.clerkStatus.value()?.channel_healths ?? [];
    return ch.map((c) => ({
      name: c.stream,
      state: c.healthy ? 'healthy' : 'unhealthy',
    }));
  }
}
