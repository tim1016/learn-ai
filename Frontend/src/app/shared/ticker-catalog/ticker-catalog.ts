import type { Signal } from '@angular/core';

import type { PriceAdjustmentMode } from '../data-lake/data-lake.types';
import type { TickerOption } from '../ticker-range-picker';

/** What one adjustment mode's slice of the lake catalog looks like to a picker. */
export interface TickerCatalogView {
  /** Instruments the lake holds runnable bars for, under this mode. */
  readonly pool: Signal<readonly TickerOption[]>;
  /** Seed recents, less any symbol this mode does not hold. */
  readonly recent: Signal<readonly string[]>;
  readonly loading: Signal<boolean>;
  /**
   * The lake has answered for this mode at least once, so `pool` is its
   * verdict — an empty pool means the tree holds nothing, not "not yet asked".
   */
  readonly resolved: Signal<boolean>;
  /** Why the catalog could not be read, or `null` when the lake answered. */
  readonly unavailable: Signal<string | null>;
  /** Re-read this mode — after a backfill, or from a failure retry. */
  reload(): void;
}

/**
 * The lake catalog, sliced by adjustment mode.
 *
 * Named as an interface so test doubles are checked against it: a `useValue`
 * provider is not type-checked against the class it replaces, so without this
 * a fake drifts silently as the service grows members.
 */
export interface TickerCatalog {
  /**
   * The catalog for one adjustment mode. Stable across calls for the same
   * mode, so a component may call it inside a `computed`.
   *
   * Mode is a parameter rather than service state on purpose: it belongs to
   * whichever page is asking (a Strategy Lab `both` run reads raw, Data Lab
   * has its own toggle), and holding it on a root-scoped singleton let one
   * page's choice outlive its own teardown and silently repoint every other
   * picker in the app.
   */
  viewFor(mode: PriceAdjustmentMode): TickerCatalogView;
}
