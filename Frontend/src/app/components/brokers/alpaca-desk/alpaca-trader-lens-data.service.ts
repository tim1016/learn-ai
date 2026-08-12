import { Injectable, inject, resource, signal } from '@angular/core';

import type {
  BrokerPortfolioHistory,
  PortfolioHistoryProof,
  PortfolioHistoryRange,
} from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';

const MAX_TODAY_ACTIVITIES = 100;

function viewerDayStartMs(now: Date): number {
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  return start.getTime();
}

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
      afterMs: viewerDayStartMs(new Date()),
      limit: MAX_TODAY_ACTIVITIES,
    }),
  });

  readonly portfolioHistory = resource<BrokerPortfolioHistory | undefined, unknown>({
    loader: () => {
      const range = this.portfolioHistoryRange();
      return range === undefined
        ? Promise.resolve(undefined)
        : this.brokers.getPortfolioHistory('alpaca', range);
    },
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
    this.portfolioHistory.reload();
    this.portfolioHistoryProof.reload();
  }
}
