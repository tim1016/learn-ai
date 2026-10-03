import { Injectable, Injector, computed, inject, resource, untracked, type ResourceRef } from '@angular/core';

import { DataLakeService, type BackfillDefaults, type DataLakeRead } from '../data-lake';
import { etIsoDate } from '../date/et-midnight';
import { UNBOUNDED, type DateBounds } from './date-bounds';
import { TradingCalendarService } from './trading-calendar.service';

/**
 * The bound of a page whose run fetches its own bars: from the oldest day the
 * provider serves to the last completed session.
 *
 * Research Lab's runs fetch from the provider directly, and Strategy Lab's
 * auto-fetch and the spec runner materialize missing days before they read,
 * so what the lake already holds does not limit them. The provider's plan
 * does. The floor is the data plane's own (``GET /backfill-defaults``); the
 * browser does not guess it.
 */
@Injectable({ providedIn: 'root' })
export class ProviderBoundsService {
  private readonly lake = inject(DataLakeService);
  private readonly calendar = inject(TradingCalendarService);
  private readonly injector = inject(Injector);
  private opened: ResourceRef<DataLakeRead<BackfillDefaults> | undefined> | null = null;

  /** Read on first ask, once per tab; ``resource()`` installs an effect, hence ``untracked``. */
  private get defaults(): ResourceRef<DataLakeRead<BackfillDefaults> | undefined> {
    this.opened ??= untracked(() => resource({ loader: () => this.lake.backfillDefaults(), injector: this.injector }));
    return this.opened;
  }

  readonly bounds = computed<DateBounds>(() => {
    const read = this.defaults.value();
    if (read === undefined) return { ...UNBOUNDED, unknown: "The provider's history is being read." };
    if (read.kind !== 'ok') return { ...UNBOUNDED, unknown: `The provider's history could not be read: ${read.message}` };
    const floor = etIsoDate(read.value.provider_history_start_ms);
    const last = this.calendar.sessions()?.lastCompleted(this.calendar.nowMs()) ?? null;
    return {
      min: floor,
      max: last,
      minReason: `The provider serves bars from ${floor}.`,
      maxReason: last === null ? null : `The last completed session is ${last}.`,
      unknown: null,
    };
  });
}
