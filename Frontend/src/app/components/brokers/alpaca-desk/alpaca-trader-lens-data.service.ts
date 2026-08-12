import { Injectable, inject, resource } from '@angular/core';

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

  readonly positions = resource({
    loader: () => this.brokers.listPositions('alpaca'),
  });

  readonly activities = resource({
    loader: () => this.brokers.listActivities('alpaca', {
      afterMs: viewerDayStartMs(new Date()),
      limit: MAX_TODAY_ACTIVITIES,
    }),
  });
}
