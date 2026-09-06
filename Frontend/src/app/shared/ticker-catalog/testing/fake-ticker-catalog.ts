import { signal, type Provider, type WritableSignal } from '@angular/core';

import type { PriceAdjustmentMode } from '../../data-lake/data-lake.types';
import type { TickerOption } from '../../ticker-range-picker';
import type { TickerCatalog } from '../ticker-catalog';
import { TickerCatalogService } from '../ticker-catalog.service';

/**
 * A `TickerCatalog` whose state a test sets directly.
 *
 * The real service reads the lake over HTTP; every picker spec cares about
 * what the pool *contains*, not how it got there, so they take this instead of
 * standing up `HttpTestingController` apiece.
 *
 * Typed as `TickerCatalog` on purpose: a `useValue` provider is not checked
 * against the class it replaces, so without the shared interface this double
 * would drift silently as the service grew members.
 */
export interface FakeTickerCatalog extends TickerCatalog {
  readonly pool: WritableSignal<readonly TickerOption[]>;
  readonly recent: WritableSignal<readonly string[]>;
  readonly loading: WritableSignal<boolean>;
  readonly unavailable: WritableSignal<string | null>;
  readonly mode: WritableSignal<PriceAdjustmentMode>;
  reloadCount: number;
}

export function fakeTickerCatalog(
  pool: readonly TickerOption[] = [],
  recent: readonly string[] = [],
): FakeTickerCatalog {
  return {
    pool: signal<readonly TickerOption[]>(pool),
    recent: signal<readonly string[]>(recent),
    loading: signal(false),
    unavailable: signal<string | null>(null),
    mode: signal<PriceAdjustmentMode>('polygon_split_adjusted'),
    reloadCount: 0,
    useMode(mode: PriceAdjustmentMode): void {
      this.mode.set(mode);
    },
    reload(): void {
      this.reloadCount++;
    },
  };
}

export function provideFakeTickerCatalog(catalog: FakeTickerCatalog): Provider {
  return { provide: TickerCatalogService, useValue: catalog };
}
