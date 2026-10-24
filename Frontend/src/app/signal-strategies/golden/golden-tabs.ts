import { GOLDEN_QUALIFICATION_QUERY_PARAM } from '../../fleet/account-workspace';
import { defineWorkspaceTabs } from '../../shared/workspace/workspace-tabs';
import { SIGNAL_STRATEGIES_ROUTE, canBeStrategyKey } from '../signal-strategy-tabs';

/**
 * A strategy's Golden tab, declared once (ADR 0084, ADR 0085): a strategy's address
 * is `/signal-strategies/:key/golden/(studies|versions|configuration)`, and one
 * study is `/golden/studies/:studyId`. Settings are approved only here, and each
 * home holds one kind of evidence:
 *
 * - **Studies** — Golden Search: lock a plan, search, test over time, compare, one
 *   final test and the decision (ADR 0074).
 * - **Versions** — the immutable qualified versions Golden Search approved, per
 *   symbol, with the active default and each version's own status.
 * - **Configuration review** — a selected baseline run and the exact-configuration
 *   human review under ADR 0061.
 */
export type GoldenTabId = 'studies' | 'versions' | 'configuration';

export const GOLDEN_TABS = defineWorkspaceTabs<GoldenTabId>(
  { studies: 'Studies', versions: 'Versions', configuration: 'Configuration review' },
  ['studies', 'versions', 'configuration'],
);

/**
 * The query keys Golden's pages read. `run`, `search` and `walkForward` are the Explore
 * evidence a plan opens from, by the evidence's own native id, so a handoff never renames
 * them; the studies page and the handoff read `search` and `walkForward` by those names.
 */
export const GOLDEN_RUN_PARAM = 'run';
/** The selected baseline run's review to open in Configuration review. */
export const GOLDEN_CASE_PARAM = 'case';
/** Opens the study list's History tab; a study to revise opens the plan. */
export const GOLDEN_TAB_PARAM = 'tab';
export const GOLDEN_REVISE_PARAM = 'revise';

/** Router commands for one of a strategy's Golden pages. */
export function goldenTabRoute(strategyKey: string, tab: GoldenTabId): readonly string[] {
  return [SIGNAL_STRATEGIES_ROUTE, strategyKey, 'golden', tab];
}

/** Router commands for one Golden Search study, under the strategy it ran for. */
export function goldenStudyRoute(strategyKey: string, studyId: string): readonly string[] {
  return [...goldenTabRoute(strategyKey, 'studies'), studyId];
}

/** The strategy's Proof: the program's proof and its flag. */
export function proofRoute(strategyKey: string): readonly string[] {
  return [SIGNAL_STRATEGIES_ROUTE, strategyKey, 'proof'];
}

export interface GoldenLocation {
  readonly tab: GoldenTabId | null;
  /** The study an address opens, or `null` for the list. */
  readonly studyId: string | null;
}

/** The Golden page a URL names, or `null` for any address outside a strategy's Golden tab. */
export function goldenLocation(url: string): GoldenLocation | null {
  const path = url.split(/[?#]/)[0];
  const [root, key, workspaceTab, page, studyId] = path.split('/').filter((segment) => segment !== '');
  if (`/${root}` !== SIGNAL_STRATEGIES_ROUTE || key === undefined || !canBeStrategyKey(key) || workspaceTab !== 'golden') return null;
  return {
    tab: GOLDEN_TABS.tabs.find((tab) => tab.id === page)?.id ?? null,
    studyId: page === 'studies' && studyId !== undefined ? decodeURIComponent(studyId) : null,
  };
}

/**
 * What "Use in Deploy" carries to the account list (`/brokers/alpaca`): the
 * strategy the version was approved for and the exact version, under Deploy's
 * own `strategy` and `golden_qualification` keys. Deploy applies the version's
 * strategy, symbol and settings from the version itself; the account is still the
 * owner's choice there, and nothing is ever picked for them.
 */
export function deployHandoffQuery(strategyKey: string, qualificationId: string | null): Record<string, string> {
  return {
    strategy: strategyKey,
    ...(qualificationId === null ? {} : { [GOLDEN_QUALIFICATION_QUERY_PARAM]: qualificationId }),
  };
}
