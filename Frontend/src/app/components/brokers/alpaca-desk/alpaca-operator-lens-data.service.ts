import { Injectable, computed, inject, resource, signal } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';

/**
 * Caches the operator-only evidence for the lifetime of the desk shell.
 *
 * The Trader lens never instantiates this data: both reads stay dormant until
 * the shell has selected Operator. The Clerk status supplies the account
 * identity for the evidence-bound SQLite projection, so the second request
 * cannot accidentally inspect a different account.
 */
@Injectable()
export class AlpacaOperatorLensDataService {
  private readonly brokers = inject(BrokersService);
  private readonly requested = signal(false);

  readonly status = resource({
    params: () => (this.requested() ? 'alpaca' : undefined),
    loader: ({ params }) => this.brokers.getClerkStatus(params),
  });

  private readonly sqliteAccountId = computed(() => {
    const status = this.status.value();
    return this.requested() && status?.authority_kind === 'sqlite'
      ? status.account_id
      : undefined;
  });

  /** Backend-authored dominant guidance and exact recovery capabilities. */
  readonly projection = resource({
    params: () => this.sqliteAccountId(),
    loader: ({ params }) => this.brokers.getSqliteClerkProjection(params),
  });

  loadOnce(): void {
    this.requested.set(true);
  }
}
