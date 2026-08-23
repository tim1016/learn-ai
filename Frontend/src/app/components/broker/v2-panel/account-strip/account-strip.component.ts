import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { Router } from '@angular/router';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import type { AccountOperatorPosture, OperatorMove } from '../../../../api/operator-blocker.types';
import { accountOperatorPostureBlocker, movesForBlocker } from '../../../../api/operator-blocker.types';
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
  /**
   * The fleet_roster projection's own navigate move, if it declared one —
   * e.g. `fix_elsewhere`'s "Open Account Operator desk". Only `navigate`
   * is self-dispatchable here: this strip has no recovery panel of its own
   * to open a `confirm_in_form` anchor against, and no runbook viewer
   * exists to route an `open_runbook` move to (2026-08-20 review).
   */
  readonly move: OperatorMove | null;
}

/**
 * Pure account-level posture for the bots fleet container. The single
 * "Account status" fact renders only the backend-authored `fleet_roster`
 * projection of `ClerkStatus.operator_posture` (issue #1664) — it never
 * combines `trading_blocked`/`account_blocked`/`freeze`/`hold` into a
 * client-derived verdict, and it renders that projection's declared move,
 * not just its problem statement.
 *
 * The triage header shows a one-line summary (account, mode, equity, buying
 * power, reconciliation). Broker status, cash, channels, and the operator
 * posture move sit behind a disclosure rather than being dropped — the
 * compact header has no room for them, but the move is the only
 * account-recovery route this surface can dispatch. Freeze and hold alerts
 * stay outside the disclosure because they gate order-producing activity.
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
  private readonly router = inject(Router, { optional: true });
  readonly account = input<BrokerAccountSnapshot | null>(null);
  readonly clerkStatus = input<ClerkStatus | null>(null);
  readonly loading = input(false);
  readonly refreshing = input(false);
  readonly accountUnavailable = input(false);
  readonly clerkUnavailable = input(false);

  protected readonly fmtCurrency = fmtCurrency;
  protected readonly expanded = signal(false);

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
    if (blocker === null) {
      return { headline: posture.status_headline, detail: posture.status_detail, move: null };
    }
    const [move = null] = movesForBlocker(blocker).filter((candidate) => candidate.action.kind === 'navigate');
    return { headline: blocker.headline, detail: blocker.detail ?? null, move };
  });

  protected toggleExpanded(): void {
    this.expanded.update((open) => !open);
  }

  protected requestMove(move: OperatorMove): void {
    if (move.action.kind !== 'navigate' || this.router === null) return;
    void this.router.navigate([move.action.route], { fragment: move.action.fragment ?? undefined });
  }
}
