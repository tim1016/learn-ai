import { Injectable, inject, resource, signal } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';

/** Caches the operator-only status for the lifetime of the desk shell. */
@Injectable()
export class AlpacaOperatorLensDataService {
  private readonly brokers = inject(BrokersService);
  private readonly requested = signal(false);

  readonly status = resource({
    params: () => (this.requested() ? 'alpaca' : undefined),
    loader: ({ params }) => this.brokers.getClerkStatus(params),
  });

  loadOnce(): void {
    this.requested.set(true);
  }
}
