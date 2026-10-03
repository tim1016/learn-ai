import { Injectable, Injector, computed, inject, resource, untracked, type ResourceRef } from '@angular/core';

import { DataLakeService, type BackfillDefaults, type DataLakeRead } from '../data-lake';
import { etIsoDate } from '../date/et-midnight';
import type { IsoDate } from '../date/iso-date';
import { UNBOUNDED, type DateBounds } from './date-bounds';
import { TradingCalendarService } from './trading-calendar.service';

/** The provider's bound: from its oldest day to ``last``; with no ``last``, open-ended. */
export function providerBounds(floor: IsoDate, last: IsoDate | null): DateBounds {
  return {
    min: floor,
    max: last,
    minReason: `The provider serves bars from ${floor}.`,
    maxReason: last === null ? null : `The last completed session is ${last}.`,
    unknown: null,
  };
}

/**
 * The bound of a page whose run fetches its own bars.
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

  /** The oldest day the provider serves, or why it is not known. */
  private readonly floor = computed<{ readonly day: IsoDate } | { readonly unknown: string }>(() => {
    const read = this.defaults.value();
    if (read === undefined) return { unknown: "The provider's history is being read." };
    if (read.kind !== 'ok') return { unknown: `The provider's history could not be read: ${read.message}` };
    return { day: etIsoDate(read.value.provider_history_start_ms) };
  });

  /** For a run over completed sessions: the provider's oldest day to the last completed session. */
  readonly bounds = computed<DateBounds>(() => {
    const floor = this.floor();
    return 'day' in floor ? providerBounds(floor.day, this.calendar.lastCompleted()) : { ...UNBOUNDED, unknown: floor.unknown };
  });

  /** For a page that also shows the session still forming: the provider's oldest day, with no last day. */
  readonly openEnded = computed<DateBounds>(() => {
    const floor = this.floor();
    return 'day' in floor ? providerBounds(floor.day, null) : { ...UNBOUNDED, unknown: floor.unknown };
  });
}
