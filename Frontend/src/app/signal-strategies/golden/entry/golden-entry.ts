import type { ActivatedRouteSnapshot, RedirectCommand } from '@angular/router';

import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';

/**
 * Where an old address, or an evidence address, was meant to land: a strategy's
 * Golden studies, its Proof or its Golden configuration review.
 */
export type GoldenEntryTarget = 'studies' | 'proof' | 'configuration';

/**
 * Why an old address, or an evidence address, could not be sent to a strategy's
 * Golden or Proof. Each says what the address named and what the stored evidence
 * said, and none picks a strategy the evidence did not name.
 */
export type GoldenEntryRefusal =
  | { readonly reason: 'study-not-found'; readonly studyId: string }
  /** The study exists, but the strategy it stored is not in the registry (retired or renamed). */
  | { readonly reason: 'study-unresolved'; readonly studyId: string; readonly storedStrategy: string }
  | { readonly reason: 'study-mismatch'; readonly studyId: string; readonly owner: string; readonly claimed: string }
  | { readonly reason: 'read-failed'; readonly subject: string; readonly message: string };

/**
 * What an address that has no strategy of its own resolves to when it is not
 * redirected: a chooser of registered strategies, or a refusal. Both keep the
 * address the owner arrived at.
 */
export type GoldenEntry =
  | {
      readonly kind: 'choose';
      readonly target: GoldenEntryTarget;
      /** A strategy the address named that the registry does not list. */
      readonly unknownStrategy: string | null;
      readonly strategies: readonly StrategyRegistryEntry[];
    }
  | {
      readonly kind: 'refused';
      readonly target: GoldenEntryTarget;
      readonly refusal: GoldenEntryRefusal;
    };

/**
 * A resolver for one old or evidence address: it ends the navigation on the owning
 * strategy's page (a `RedirectCommand`), or hands the page what to say. It runs in
 * an injection context, and calls `inject()` before its first `await`.
 */
export type GoldenEntryResolver = (route: ActivatedRouteSnapshot) => Promise<GoldenEntry | RedirectCommand>;
