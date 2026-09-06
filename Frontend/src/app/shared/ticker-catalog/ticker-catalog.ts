import type { Signal } from '@angular/core';

import type { PriceAdjustmentMode } from '../data-lake/data-lake.types';
import type { TickerOption } from '../ticker-range-picker';

/**
 * What a picker needs from the instrument catalog.
 *
 * Named as an interface so the test double is checked against it — a
 * `useValue` provider is not type-checked against the class it replaces, so
 * without this a fake silently drifts as the service grows members.
 */
export interface TickerCatalog {
  /** Instruments the lake holds bars for, under `mode`. */
  readonly pool: Signal<readonly TickerOption[]>;
  /** Seed recents, less any symbol the lake does not hold. */
  readonly recent: Signal<readonly string[]>;
  readonly loading: Signal<boolean>;
  /** Why the catalog could not be read, or `null` when the lake answered. */
  readonly unavailable: Signal<string | null>;
  /** The adjustment mode the pool currently describes. */
  readonly mode: Signal<PriceAdjustmentMode>;
  /** Ask for a different tree. Idempotent for the mode already loaded. */
  useMode(mode: PriceAdjustmentMode): void;
  reload(): void;
}
