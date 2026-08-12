import { Injectable, inject, resource, signal } from '@angular/core';

import type { PortfolioHistoryProof, PortfolioHistoryRange } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';

const MAX_TODAY_ACTIVITIES = 100;

/** Trader-lens reads, scoped so the hero and positions table share one fetch. */
@Injectable()
export class AlpacaTraderLensDataService {
  private readonly brokers = inject(BrokersService);
  private readonly portfolioHistoryRange = signal<PortfolioHistoryRange | undefined>(undefined);

  readonly positions = resource({
    loader: () => this.brokers.listPositions('alpaca'),
  });

  readonly activities = resource({
    loader: () => this.brokers.listActivities('alpaca', {
      currentSession: true,
      limit: MAX_TODAY_ACTIVITIES,
    }),
  });

  readonly portfolioHistoryProof = resource<PortfolioHistoryProof | undefined, unknown>({
    loader: () => {
      const range = this.portfolioHistoryRange();
      return range === undefined
        ? Promise.resolve(undefined)
        : this.brokers.getPortfolioHistoryProof('alpaca', range);
    },
  });

  selectPortfolioHistoryRange(range: PortfolioHistoryRange | undefined): void {
    this.portfolioHistoryRange.set(range);
    this.portfolioHistoryProof.reload();
  }
}
