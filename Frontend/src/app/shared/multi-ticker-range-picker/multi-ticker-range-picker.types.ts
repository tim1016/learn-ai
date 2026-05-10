/**
 * Types for the shared <app-multi-ticker-range-picker>.
 *
 * Sibling of TickerRange that carries a *universe* of symbols. Used by
 * batch-runner / cross-sectional research consumers. Out of v1: per-
 * ticker availability strip, smart advisories, cache hint — those are
 * single-symbol concepts that don't generalize.
 */

import type {
  Resolution,
  Session,
} from '../ticker-range-picker/ticker-range-picker.types';

export interface MultiTickerRange {
  /** Selected universe; UI enforces min length 1. */
  symbols: string[];
  /** YYYY-MM-DD */
  from: string;
  /** YYYY-MM-DD */
  to: string;
  resolution: Resolution;
  /** See ``TickerRange.multiplier``. */
  multiplier?: number;
  /** Defaults to ``rth`` when absent. */
  session?: Session;
  autoFetch?: boolean;
}
