import { Injectable, inject, resource, signal } from '@angular/core';

import type { PortfolioHistoryProof, PortfolioHistoryRange } from '../../../api/alpaca.types';
import type { ResourceTarget } from '../../../fleet/resource-target';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';

const MAX_TODAY_ACTIVITIES = 100;

/** Trader-lens reads, scoped so the hero and positions table share one fetch. */
@Injectable()
export class AlpacaTraderLensDataService {
  private readonly brokers = inject(BrokersService);
  private readonly deskAccount = inject(AlpacaDeskAccountDataService);
  private readonly portfolioHistoryRange = signal<PortfolioHistoryRange | undefined>(undefined);

  readonly positions = resource({
    params: () => this.deskAccount.target(),
    loader: ({ params }) => params === null
      ? Promise.reject(new Error('The desk route does not name a lane.'))
      : this.brokers.listPositions(params),
  });

  readonly activities = resource({
    params: () => this.deskAccount.target(),
    loader: ({ params }) => params === null
      ? Promise.reject(new Error('The desk route does not name a lane.'))
      : this.brokers.listActivities(params, {
      currentSession: true,
      limit: MAX_TODAY_ACTIVITIES,
    }),
  });

  readonly portfolioHistoryProof = resource<
    PortfolioHistoryProof | undefined,
    { readonly target: ResourceTarget | null; readonly range: PortfolioHistoryRange | undefined }
  >({
    params: () => ({ target: this.deskAccount.target(), range: this.portfolioHistoryRange() }),
    loader: ({ params }) => {
      return params.range === undefined || params.target === null
        ? Promise.resolve(undefined)
        : this.brokers.getPortfolioHistoryProof(params.target, params.range);
    },
  });

  selectPortfolioHistoryRange(range: PortfolioHistoryRange | undefined): void {
    this.portfolioHistoryRange.set(range);
    this.portfolioHistoryProof.reload();
  }
}
