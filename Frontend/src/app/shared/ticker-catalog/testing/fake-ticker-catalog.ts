import { signal, type Provider } from '@angular/core';

import type { TickerOption } from '../../ticker-range-picker';
import { TickerCatalogService } from '../ticker-catalog.service';

/**
 * A `TickerCatalogService` whose state a test sets directly.
 *
 * The real service reads the lake over HTTP; every picker spec cares about
 * what the pool *contains*, not how it got there, so they take this instead
 * of standing up `HttpTestingController` apiece.
 */
export interface FakeTickerCatalog {
  readonly pool: ReturnType<typeof signal<readonly TickerOption[]>>;
  readonly recent: ReturnType<typeof signal<readonly string[]>>;
  readonly loading: ReturnType<typeof signal<boolean>>;
  readonly unavailable: ReturnType<typeof signal<string | null>>;
  reloadCount: number;
  reload(): void;
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
    reloadCount: 0,
    reload(): void {
      this.reloadCount++;
    },
  };
}

export function provideFakeTickerCatalog(catalog: FakeTickerCatalog): Provider {
  return { provide: TickerCatalogService, useValue: catalog };
}
