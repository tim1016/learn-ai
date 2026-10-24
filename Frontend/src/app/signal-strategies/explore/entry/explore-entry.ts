import type { ActivatedRouteSnapshot, RedirectCommand } from '@angular/router';

import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';
import type { ExploreTabId } from '../explore-tabs';

/**
 * Why an old address, or an evidence address, could not be sent to a strategy's
 * Explore. Each says what the address named and what the stored evidence said,
 * and none picks a strategy the evidence did not name.
 */
export type ExploreEntryRefusal =
  | { readonly reason: 'invalid-run'; readonly raw: string }
  | { readonly reason: 'run-not-found'; readonly runId: number }
  | { readonly reason: 'run-mismatch'; readonly runId: number; readonly owner: string; readonly claimed: string }
  | { readonly reason: 'search-not-found'; readonly searchId: string }
  | { readonly reason: 'search-unresolved'; readonly searchId: string; readonly storedStrategy: string }
  | { readonly reason: 'search-mismatch'; readonly searchId: string; readonly owner: string; readonly claimed: string }
  | { readonly reason: 'read-failed'; readonly subject: string; readonly message: string };

/**
 * What an address that has no strategy of its own resolves to when it is not
 * redirected: a chooser of registered strategies, a refusal, or a run no strategy
 * holds. Each keeps the address the owner arrived at.
 */
export type ExploreEntry =
  | {
      readonly kind: 'choose';
      /** The Explore tool the address wanted. */
      readonly target: ExploreTabId;
      /** A strategy the address named that the registry does not list. */
      readonly unknownStrategy: string | null;
      readonly strategies: readonly StrategyRegistryEntry[];
      /** The address's query, less the keys the new address restates: what each strategy's link carries on. */
      readonly query: Readonly<Record<string, string>>;
    }
  | {
      readonly kind: 'refused';
      readonly target: ExploreTabId;
      readonly refusal: ExploreEntryRefusal;
    }
  /**
   * A saved run whose stored strategy the registry does not list (retired,
   * renamed, or a custom LEAN source): no Explore holds it, so it is shown by
   * itself, read-only, under the strategy name it stored.
   */
  | {
      readonly kind: 'run-report';
      readonly target: 'backtest';
      readonly runId: number;
      readonly storedStrategy: string;
    };

/**
 * A resolver for one old or evidence address: it ends the navigation on the
 * owning strategy's Explore (a `RedirectCommand`), or hands the page what to say.
 * It runs in an injection context, and calls `inject()` before its first `await`.
 */
export type ExploreEntryResolver = (route: ActivatedRouteSnapshot) => Promise<ExploreEntry | RedirectCommand>;
