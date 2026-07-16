import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';

import type { AccountTriageVerdictMove } from '../../../api/account-reconciliation.types';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { fmtCurrency, fmtDurationRemaining, fmtSignedCurrency } from '../format';
import { AccountDeskHoldingsStore } from './account-desk-holdings-store.service';
import { AccountDeskEventsStore } from './account-desk-events-store.service';
import { AccountDeskOperatorEventsComponent } from './account-desk-operator-events.component';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';
import { AccountDeskTraderEventsComponent } from './account-desk-trader-events.component';
import { AccountDeskTraderHoldingsComponent } from './account-desk-trader-holdings.component';

type AccountDeskLens = 'trader' | 'operator';

/** Account-id route host for the shared verdict spine and the later desk lenses. */
@Component({
  selector: 'app-account-desk-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskTraderHoldingsComponent,
    AccountDeskTraderEventsComponent,
    AccountDeskOperatorEventsComponent,
    PageHeaderComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './account-desk-page.component.html',
  styleUrl: './account-desk-page.component.scss',
})
export class AccountDeskPageComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  readonly store = inject(AccountDeskSurfaceStore);
  readonly holdings = inject(AccountDeskHoldingsStore);
  readonly events = inject(AccountDeskEventsStore);
  readonly lens = signal<AccountDeskLens>('trader');
  private readonly nowMs = signal(Date.now());

  readonly triage = this.store.triage;
  readonly loading = this.store.loading;
  readonly error = this.store.error;
  readonly showingStaleLastGood = this.store.showingStaleLastGood;
  readonly headlineMetrics = this.holdings.headlineMetrics;
  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly displayAccountId = computed(() => this.triage()?.account_id ?? this.store.accountId());
  readonly freshnessCountdown = computed(() => {
    const validUntilMs = this.triage()?.account_observation.valid_until_ms;
    return validUntilMs === null || validUntilMs === undefined
      ? null
      : fmtDurationRemaining(validUntilMs - this.nowMs());
  });

  constructor() {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const accountId = params.get('accountId');
      if (accountId) {
        void this.store.load(accountId);
        void this.holdings.load(accountId);
        void this.events.load(accountId);
      }
    });
    const intervalId = window.setInterval(() => this.nowMs.set(Date.now()), 1_000);
    this.destroyRef.onDestroy(() => window.clearInterval(intervalId));
  }

  selectLens(lens: AccountDeskLens): void {
    this.lens.set(lens);
  }

  retry(): void {
    this.store.retry();
  }

  followPrimaryMove(move: AccountTriageVerdictMove): void {
    void this.router.navigate([move.route], { fragment: move.fragment ?? undefined });
  }
}
