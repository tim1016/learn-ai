/**
 * Hero + foldable-rest combo classification (design spec D5). A strategy's
 * hero combo (highest Python-authored total_pnl, from recencyHero) shows
 * by default; every other combo for that (symbol, strategy) pair only
 * shows once the user expands that group.
 */

export interface HeroKey {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
}

interface ComboIdentity {
  symbol: string;
  strategyKey: string;
  paramsHash: string;
}

export function groupKey(symbol: string, strategyKey: string): string {
  return `${symbol}::${strategyKey}`;
}

export function isHeroTrade(trade: ComboIdentity, heroes: readonly HeroKey[]): boolean {
  return heroes.some(
    (h) => h.symbol === trade.symbol && h.strategyKey === trade.strategyKey && h.paramsHash === trade.paramsHash,
  );
}

export function filterToHeroAndExpanded<T extends ComboIdentity>(
  trades: T[],
  heroes: readonly HeroKey[],
  expandedGroups: ReadonlySet<string>,
): T[] {
  return trades.filter(
    (t) => isHeroTrade(t, heroes) || expandedGroups.has(groupKey(t.symbol, t.strategyKey)),
  );
}
