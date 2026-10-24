import { inject } from '@angular/core';
import { RedirectCommand, Router, type ActivatedRouteSnapshot } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { GridSearchService } from '../../../components/grid-search/grid-search.service';
import { BacktestRunsService } from '../../../services/backtest-runs.service';
import { failureText, isNotFound, nonBlank, registered } from '../../entry-support';
import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';
import { SignalStrategyRegistryService } from '../../signal-strategy-registry.service';
import { EXPLORE_RUN_PARAM, EXPLORE_SEARCH_PARAM, exploreTabRoute, parseRunId, type ExploreTabId } from '../explore-tabs';
import type { ExploreEntry, ExploreEntryRefusal, ExploreEntryResolver } from './explore-entry';

/** The query keys an old address carried only to name the strategy or the run; the new address says them in its path or restates them. */
const RESTATED = new Set(['strategy', EXPLORE_RUN_PARAM, 'restoreRun', EXPLORE_SEARCH_PARAM]);

/**
 * One redirect, straight to the strategy's Explore: the old address's query and
 * fragment go with it (less the keys the new address restates), and the id the
 * owner was resolved from rides in the tool's own native query key.
 */
function redirectTo(
  route: ActivatedRouteSnapshot,
  router: Router,
  strategyKey: string,
  tool: ExploreTabId,
  native: Readonly<Record<string, string>> = {},
): RedirectCommand {
  const kept = Object.fromEntries(Object.entries(route.queryParams).filter(([name]) => !RESTATED.has(name)));
  return new RedirectCommand(
    router.createUrlTree([...exploreTabRoute(strategyKey, tool)], {
      queryParams: { ...kept, ...native },
      fragment: route.fragment ?? undefined,
    }),
  );
}

function refused(target: ExploreTabId, refusal: ExploreEntryRefusal): ExploreEntry {
  return { kind: 'refused', target, refusal };
}

/**
 * The owner of a saved backtest run, from the strategy the run itself stored.
 * `/strategy-lab`, the run-detail aliases and the evidence address all end here:
 * a run whose stored strategy the registry lists lands on that strategy's
 * Backtest; one whose strategy it does not list, one that does not exist and one
 * the address claims for another strategy are each refused in place. An address
 * naming no run goes to the strategy it names, or to a chooser. No strategy is
 * ever guessed.
 */
export const resolveBacktestEntry: ExploreEntryResolver = async (route) => {
  const router = inject(Router);
  const registry = inject(SignalStrategyRegistryService);
  const runs = inject(BacktestRunsService);
  const target: ExploreTabId = 'backtest';

  let entries: readonly StrategyRegistryEntry[];
  try {
    entries = await registry.entries();
  } catch (error) {
    return refused(target, { reason: 'read-failed', subject: 'the strategy registry', message: failureText(error) });
  }

  const claimed = nonBlank(route.queryParamMap.get('strategy'));
  const rawRun = route.paramMap.get('id') ?? route.queryParamMap.get(EXPLORE_RUN_PARAM) ?? route.queryParamMap.get('restoreRun');
  if (rawRun !== null) {
    const runId = parseRunId(rawRun);
    if (runId === null) return refused(target, { reason: 'invalid-run', raw: rawRun });
    let run;
    try {
      run = await firstValueFrom(runs.get(runId));
    } catch (error) {
      return refused(target, { reason: 'read-failed', subject: `saved run #${runId}`, message: failureText(error) });
    }
    if (run === null) return refused(target, { reason: 'run-not-found', runId });
    const owner = registered(entries, run.strategyName);
    if (owner === undefined) return refused(target, { reason: 'run-unresolved', runId, storedStrategy: run.strategyName });
    if (claimed !== null && claimed !== owner.strategy_key) {
      return refused(target, { reason: 'run-mismatch', runId, owner: owner.strategy_key, claimed });
    }
    return redirectTo(route, router, owner.strategy_key, target, { [EXPLORE_RUN_PARAM]: String(runId) });
  }

  if (claimed !== null) {
    const named = registered(entries, claimed);
    if (named !== undefined) return redirectTo(route, router, named.strategy_key, target);
    return { kind: 'choose', target, unknownStrategy: claimed, strategies: entries };
  }
  return { kind: 'choose', target, unknownStrategy: null, strategies: entries };
};

/**
 * The owner of a saved Grid Search, from the strategy the search stored, for
 * Sweep or Walk-forward. `/grid-search`, `/walk-forward` and the evidence
 * address end here, with the same rules as a backtest run: a search names its
 * owner, an address with no search goes to the strategy it names or to a
 * chooser, and nothing is guessed.
 */
function resolveSearchEntry(target: Extract<ExploreTabId, 'sweep' | 'walk-forward'>): ExploreEntryResolver {
  return async (route) => {
    const router = inject(Router);
    const registry = inject(SignalStrategyRegistryService);
    const searches = inject(GridSearchService);

    let entries: readonly StrategyRegistryEntry[];
    try {
      entries = await registry.entries();
    } catch (error) {
      return refused(target, { reason: 'read-failed', subject: 'the strategy registry', message: failureText(error) });
    }

    const claimed = nonBlank(route.queryParamMap.get('strategy'));
    const searchId = nonBlank(route.paramMap.get('id') ?? route.queryParamMap.get(EXPLORE_SEARCH_PARAM));
    if (searchId !== null) {
      let search;
      try {
        search = await searches.get(searchId);
      } catch (error) {
        if (isNotFound(error)) return refused(target, { reason: 'search-not-found', searchId });
        return refused(target, { reason: 'read-failed', subject: `grid search ${searchId}`, message: failureText(error) });
      }
      const owner = registered(entries, search.strategy_key);
      if (owner === undefined) return refused(target, { reason: 'search-unresolved', searchId, storedStrategy: search.strategy_key });
      if (claimed !== null && claimed !== owner.strategy_key) {
        return refused(target, { reason: 'search-mismatch', searchId, owner: owner.strategy_key, claimed });
      }
      return redirectTo(route, router, owner.strategy_key, target, { [EXPLORE_SEARCH_PARAM]: searchId });
    }

    if (claimed !== null) {
      const named = registered(entries, claimed);
      if (named !== undefined) return redirectTo(route, router, named.strategy_key, target);
      return { kind: 'choose', target, unknownStrategy: claimed, strategies: entries };
    }
    return { kind: 'choose', target, unknownStrategy: null, strategies: entries };
  };
}

export const resolveSweepEntry: ExploreEntryResolver = resolveSearchEntry('sweep');
export const resolveWalkForwardEntry: ExploreEntryResolver = resolveSearchEntry('walk-forward');
