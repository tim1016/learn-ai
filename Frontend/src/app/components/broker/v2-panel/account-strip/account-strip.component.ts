import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  InjectionToken,
  computed,
  effect,
  inject,
  input,
  signal,
  type Signal,
  type WritableSignal,
} from '@angular/core';
import { Router } from '@angular/router';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import type { AccountOperatorPosture, OperatorMove } from '../../../../api/operator-blocker.types';
import { accountOperatorPostureBlocker, movesForBlocker } from '../../../../api/operator-blocker.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { fmtCurrency } from '../../format';
import { AccountPostureDetailComponent } from './account-posture-detail.component';
import type {
  AccountStatusView,
  ChannelPosture,
} from './account-posture-detail.component';

/** How long a refresh-failure pill stays visible before auto-dismissing. */
export const REFRESH_PILL_DISMISS_MS = new InjectionToken<number>('REFRESH_PILL_DISMISS_MS', {
  factory: () => 6000,
});


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
  imports: [ReceiptLabelPipe, TimestampDisplayComponent, AccountPostureDetailComponent],
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

  private readonly pillDismissMs = inject(REFRESH_PILL_DISMISS_MS);
  private readonly destroyRef = inject(DestroyRef);
  private readonly pillTimers = new Map<'account' | 'clerk', ReturnType<typeof setTimeout>>();

  /**
   * Refresh failures surface as transient pills, not standing banners: the
   * strip keeps rendering the last-good observation (whose timestamp already
   * conveys staleness), so a failed poll is an event to note, not a state to
   * dwell on. Each false→true edge of an unavailable input shows its pill
   * once; a persistently failing poll does not re-nag.
   */
  protected readonly accountPillVisible = signal(false);
  protected readonly clerkPillVisible = signal(false);

  constructor() {
    this.showPillOnRisingEdge(this.accountUnavailable, 'account', this.accountPillVisible);
    this.showPillOnRisingEdge(this.clerkUnavailable, 'clerk', this.clerkPillVisible);
    this.destroyRef.onDestroy(() => this.pillTimers.forEach((timer) => clearTimeout(timer)));
  }

  private showPillOnRisingEdge(
    source: Signal<boolean>,
    key: 'account' | 'clerk',
    visible: WritableSignal<boolean>,
  ): void {
    let previous = false;
    effect(() => {
      const unavailable = source();
      if (unavailable && !previous) {
        visible.set(true);
        clearTimeout(this.pillTimers.get(key));
        this.pillTimers.set(
          key,
          setTimeout(() => visible.set(false), this.pillDismissMs),
        );
      }
      previous = unavailable;
    });
  }

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
