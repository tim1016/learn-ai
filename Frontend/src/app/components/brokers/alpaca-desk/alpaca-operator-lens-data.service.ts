import { Injectable, computed, inject, resource, signal } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';

/**
 * Caches the operator-only evidence for the lifetime of the desk shell.
 *
 * The Trader lens never instantiates this data: both reads stay dormant until
 * the shell has selected Operator. The Clerk status supplies the account
 * identity for the evidence-bound SQLite projection, so the second request
 * cannot accidentally inspect a different account.
 *
 * `status` and `projection` are bound to the same `projectionRefreshVersion`
 * trigger. `status` carries `operator_posture` — the dominant posture card's
 * sole data source — so a recovery action that only invalidated `projection`
 * without also reloading `status` would leave that card showing a stale (or
 * missing a newly created) blocker for the rest of the desk's lifetime
 * (2026-08-20 review).
 */
/**
 * Clerk authorities whose status names a real account-scoped SQLite projection.
 *
 * Mirrors the backend's own closed set (`SQLITE_FACADE_AUTHORITIES` in
 * `active_runtime.py`). A shadow authority reads a live account through a
 * `shadow:` custody id and still has a SQLite projection, so gating on
 * `real_paper` alone would leave the Operator lens dark on exactly the
 * real-money-adjacent account it most needs to describe (ADR 0059 D2), and
 * `real_live` once the live authority is installed (slice 7).
 */
const SQLITE_PROJECTION_AUTHORITIES: ReadonlySet<string> = new Set(['real_paper', 'shadow', 'real_live']);

@Injectable()
export class AlpacaOperatorLensDataService {
  private readonly brokers = inject(BrokersService);
  private readonly requested = signal(false);
  readonly projectionRefreshVersion = signal(0);

  readonly status = resource({
    params: () => (this.requested() ? this.projectionRefreshVersion() : undefined),
    loader: () => this.brokers.getClerkStatus('alpaca'),
  });

  private readonly sqliteAccountId = computed(() => {
    const status = this.status.value();
    return this.requested() && SQLITE_PROJECTION_AUTHORITIES.has(status?.authority_kind ?? '')
      ? status?.account_id
      : undefined;
  });

  /** Backend-authored dominant guidance and exact recovery capabilities. */
  readonly projection = resource({
    params: () => {
      const accountId = this.sqliteAccountId();
      return accountId === undefined
        ? undefined
        : { accountId, refreshVersion: this.projectionRefreshVersion() };
    },
    loader: ({ params }) => this.brokers.getSqliteClerkProjection(params.accountId),
  });

  loadOnce(): void {
    this.requested.set(true);
  }

  /** Refresh every Desk surface bound to the current Clerk projection, including the canonical posture. */
  refreshProjection(): void {
    this.projectionRefreshVersion.update((version) => version + 1);
  }
}
