import { signal, type Provider, type WritableSignal } from '@angular/core';

import type { PriceAdjustmentMode } from '../../data-lake';
import type { TickerOption } from '../../ticker-range-picker';
import type { TickerCatalog, TickerCatalogView } from '../ticker-catalog';
import { TickerCatalogService } from '../ticker-catalog.service';

/**
 * One mode's view, with state a test sets directly.
 *
 * Typed as `TickerCatalogView` on purpose: a `useValue` provider is not
 * checked against the class it replaces, so without the shared interface this
 * double would drift silently as the service grew members.
 */
export interface FakeTickerCatalogView extends TickerCatalogView {
  readonly pool: WritableSignal<readonly TickerOption[]>;
  readonly recent: WritableSignal<readonly string[]>;
  readonly loading: WritableSignal<boolean>;
  readonly unavailable: WritableSignal<string | null>;
  reloadCount: number;
}

export interface FakeTickerCatalog extends TickerCatalog {
  /** Modes asked for, in order — pins that a page requested the right tree. */
  readonly modesRequested: PriceAdjustmentMode[];
  /** The default mode's view, which most specs are asserting on. */
  readonly view: FakeTickerCatalogView;
  viewFor(mode: PriceAdjustmentMode): FakeTickerCatalogView;
}

function makeView(
  pool: readonly TickerOption[],
  recent: readonly string[],
): FakeTickerCatalogView {
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

/**
 * A catalog whose every mode starts from the same seed. Specs that care about
 * one mode use `catalog.view`; specs that care that the *right* mode was asked
 * for read `catalog.modesRequested`.
 */
export function fakeTickerCatalog(
  pool: readonly TickerOption[] = [],
  recent: readonly string[] = [],
): FakeTickerCatalog {
  const views = new Map<PriceAdjustmentMode, FakeTickerCatalogView>();
  const modesRequested: PriceAdjustmentMode[] = [];
  const catalog: FakeTickerCatalog = {
    modesRequested,
    get view() {
      return catalog.viewFor('polygon_split_adjusted');
    },
    viewFor(mode: PriceAdjustmentMode): FakeTickerCatalogView {
      modesRequested.push(mode);
      const existing = views.get(mode);
      if (existing) return existing;
      const created = makeView(pool, recent);
      views.set(mode, created);
      return created;
    },
  };
  return catalog;
}

export function provideFakeTickerCatalog(catalog: FakeTickerCatalog): Provider {
  return { provide: TickerCatalogService, useValue: catalog };
}
