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
  /** The run exists, but the strategy it stored is not in the registry (retired, renamed, or a custom LEAN source). */
  | { readonly reason: 'run-unresolved'; readonly runId: number; readonly storedStrategy: string }
  | { readonly reason: 'run-mismatch'; readonly runId: number; readonly owner: string; readonly claimed: string }
  | { readonly reason: 'search-not-found'; readonly searchId: string }
  | { readonly reason: 'search-unresolved'; readonly searchId: string; readonly storedStrategy: string }
  | { readonly reason: 'search-mismatch'; readonly searchId: string; readonly owner: string; readonly claimed: string }
  | { readonly reason: 'read-failed'; readonly subject: string; readonly message: string };

/**
 * What an address that has no strategy of its own resolves to when it is not
 * redirected: a chooser of registered strategies, or a refusal. Both keep the
 * address the owner arrived at.
 */
export type ExploreEntry =
  | {
      readonly kind: 'choose';
      /** The Explore tool the address wanted. */
      readonly target: ExploreTabId;
      /** A strategy the address named that the registry does not list. */
      readonly unknownStrategy: string | null;
      readonly strategies: readonly StrategyRegistryEntry[];
    }
  | {
      readonly kind: 'refused';
      readonly target: ExploreTabId;
      readonly refusal: ExploreEntryRefusal;
    };

/**
 * A resolver for one old or evidence address: it ends the navigation on the
 * owning strategy's Explore (a `RedirectCommand`), or hands the page what to say.
 * It runs in an injection context, and calls `inject()` before its first `await`.
 */
export type ExploreEntryResolver = (route: ActivatedRouteSnapshot) => Promise<ExploreEntry | RedirectCommand>;
