import { inject } from '@angular/core';
import { RedirectCommand, Router, type ActivatedRouteSnapshot } from '@angular/router';

import { GoldenSearchService } from '../../../components/golden-search/golden-search.service';
import { failureText, isNotFound, nonBlank, registered } from '../../entry-support';
import { SignalStrategyRegistryService } from '../../signal-strategy-registry.service';
import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';
import { GOLDEN_CASE_PARAM, GOLDEN_REVISE_PARAM, goldenStudyRoute, goldenTabRoute, proofRoute } from '../golden-tabs';
import type { GoldenEntry, GoldenEntryRefusal, GoldenEntryResolver, GoldenEntryTarget } from './golden-entry';

/** The query key an old Golden Search address carried only to name the strategy: the new address says it in its path. */
const STUDIES_SPENT: ReadonlySet<string> = new Set(['strategy']);

/**
 * The query keys an old Strategy Validation address carried only to name the strategy or
 * the subview, or that nothing reads. `qc_backtest_id` was sent by that page's Strategy Lab
 * link and read by nothing, so it is dropped rather than carried to a page that would ignore it.
 */
const VALIDATION_SPENT: ReadonlySet<string> = new Set(['strategy', 'view', 'qc_backtest_id']);

/** `?view=` values that ask Strategy Validation's embedded configuration review rather than its proof. */
const CONFIGURATION_VIEWS: ReadonlySet<string> = new Set(['configuration', 'golden', 'review']);

function refused(target: GoldenEntryTarget, refusal: GoldenEntryRefusal): GoldenEntry {
  return { kind: 'refused', target, refusal };
}

/** The old address's query, less the keys the new address restates or nothing reads. */
function keptQuery(route: ActivatedRouteSnapshot, spent: ReadonlySet<string>): Record<string, string> {
  return Object.fromEntries(Object.entries(route.queryParams).filter(([name]) => !spent.has(name)));
}

/**
 * One redirect, straight to the strategy's page: the old address's query and
 * fragment go with it, less the keys the new address restates or nothing reads.
 */
function redirectTo(
  route: ActivatedRouteSnapshot,
  router: Router,
  commands: readonly string[],
  spent: ReadonlySet<string>,
  native: Readonly<Record<string, string>> = {},
): RedirectCommand {
  return new RedirectCommand(
    router.createUrlTree([...commands], { queryParams: { ...keptQuery(route, spent), ...native }, fragment: route.fragment ?? undefined }),
  );
}

/**
 * The owner of a Golden Search study, from the strategy the study itself stored.
 * `/golden-search`, `/golden-search/:studyId` and the evidence address end here: a
 * study whose stored strategy the registry lists lands on that strategy's Golden
 * studies (the study's own page for a study id, the plan for `?revise=`); one whose
 * strategy it does not list, one that does not exist and one the link claims for
 * another strategy are each refused in place. An address naming no study goes to
 * the strategy it names, or to a chooser. No strategy is ever guessed.
 */
export const resolveGoldenStudiesEntry: GoldenEntryResolver = async (route) => {
  const router = inject(Router);
  const registry = inject(SignalStrategyRegistryService);
  const studies = inject(GoldenSearchService);
  const target: GoldenEntryTarget = 'studies';

  let entries: readonly StrategyRegistryEntry[];
  try {
    entries = await registry.entries();
  } catch (error) {
    return refused(target, { reason: 'read-failed', subject: 'the strategy registry', message: failureText(error) });
  }

  const claimed = nonBlank(route.queryParamMap.get('strategy'));
  const pageId = nonBlank(route.paramMap.get('studyId') ?? route.paramMap.get('id'));
  const reviseId = nonBlank(route.queryParamMap.get(GOLDEN_REVISE_PARAM));
  const studyId = pageId ?? reviseId;
  if (studyId !== null) {
    let study;
    try {
      study = await studies.get(studyId);
    } catch (error) {
      if (isNotFound(error)) return refused(target, { reason: 'study-not-found', studyId });
      return refused(target, { reason: 'read-failed', subject: `golden search study ${studyId}`, message: failureText(error) });
    }
    const owner = registered(entries, study.strategy_key);
    if (owner === undefined) return refused(target, { reason: 'study-unresolved', studyId, storedStrategy: study.strategy_key });
    if (claimed !== null && claimed !== owner.strategy_key) {
      return refused(target, { reason: 'study-mismatch', studyId, owner: owner.strategy_key, claimed });
    }
    return pageId !== null
      ? redirectTo(route, router, goldenStudyRoute(owner.strategy_key, pageId), STUDIES_SPENT)
      : redirectTo(route, router, goldenTabRoute(owner.strategy_key, 'studies'), STUDIES_SPENT, { [GOLDEN_REVISE_PARAM]: studyId });
  }

  if (claimed !== null) {
    const named = registered(entries, claimed);
    if (named !== undefined) return redirectTo(route, router, goldenTabRoute(named.strategy_key, 'studies'), STUDIES_SPENT);
    return { kind: 'choose', target, unknownStrategy: claimed, strategies: entries, query: keptQuery(route, STUDIES_SPENT) };
  }
  return { kind: 'choose', target, unknownStrategy: null, strategies: entries, query: keptQuery(route, STUDIES_SPENT) };
};

/**
 * `/strategy-validation`, by subview. Its proof pipeline and human flag are the
 * strategy's Proof; its embedded Golden Validation workbench is Golden →
 * Configuration review, which a `?view=configuration` (or `golden`, `review`) or a
 * `?case=` asks for. The strategy is the address's `?strategy=`: one the registry
 * lists lands on its page, one it does not is named in a chooser, and an address
 * naming none opens the chooser, since the old page's habit of opening on the first
 * accepted strategy is a guess. Nothing is ever picked.
 */
export const resolveValidationEntry: GoldenEntryResolver = async (route) => {
  const router = inject(Router);
  const registry = inject(SignalStrategyRegistryService);

  const subview = CONFIGURATION_VIEWS.has(route.queryParamMap.get('view') ?? '') || route.queryParamMap.has(GOLDEN_CASE_PARAM) ? 'configuration' : 'proof';
  const target: GoldenEntryTarget = subview;

  let entries: readonly StrategyRegistryEntry[];
  try {
    entries = await registry.entries();
  } catch (error) {
    return refused(target, { reason: 'read-failed', subject: 'the strategy registry', message: failureText(error) });
  }

  const claimed = nonBlank(route.queryParamMap.get('strategy'));
  if (claimed !== null) {
    const named = registered(entries, claimed);
    if (named !== undefined) {
      return redirectTo(
        route,
        router,
        subview === 'proof' ? proofRoute(named.strategy_key) : goldenTabRoute(named.strategy_key, 'configuration'),
        VALIDATION_SPENT,
      );
    }
    return { kind: 'choose', target, unknownStrategy: claimed, strategies: entries, query: keptQuery(route, VALIDATION_SPENT) };
  }
  return { kind: 'choose', target, unknownStrategy: null, strategies: entries, query: keptQuery(route, VALIDATION_SPENT) };
};
