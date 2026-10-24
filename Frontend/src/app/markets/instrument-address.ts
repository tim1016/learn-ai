import { inject } from '@angular/core';
import { Router, type ParamMap, type Params, type RedirectFunction } from '@angular/router';

import { routePathOf } from '../fleet/account-workspace';
import { INSTRUMENT_TABS, type InstrumentTab } from './instrument-tabs';

/*
 * Where an instrument lives (ADR 0084): `/markets/:symbol/(chart|returns|options|data)`.
 *
 * The address is the place's whole identity. The symbol is the path; the
 * window, bar timeframe and session ride as query keys (`instrument-context`),
 * so Back, Forward and a reload keep the context. This file is the one place
 * that spells those addresses, and the one that turns a legacy address
 * (`/data-lab/explore?ticker=…`, the old news page) into one.
 */

export const MARKETS_ROUTE = '/markets';

/** The page that lists every headline for an instrument; it sits under the Chart tab. */
const HEADLINES_SEGMENT = 'news';

/** Router commands for one tab of an instrument; `rest` goes after the tab (`['news']`). */
export function instrumentCommands(symbol: string, tab: InstrumentTab, rest: readonly string[] = []): string[] {
  return [MARKETS_ROUTE, symbol, tab, ...rest];
}

/** Router commands for an instrument's all-headlines page. */
export function instrumentHeadlinesCommands(symbol: string): string[] {
  return instrumentCommands(symbol, 'chart', [HEADLINES_SEGMENT]);
}

/**
 * The query keys by which an address that names no instrument asks the Markets list for one: `then`
 * is the tab the pick opens, and `view` the view of that tab (Options has views). Every other key
 * the address carried rides along to the pick.
 */
export const PICK_TAB_KEY = 'then';
export const PICK_VIEW_KEY = 'view';

/** The keys an address that named no instrument carried for it: every key but the two that ask for the pick, as they were. */
export function keysForPick(query: ParamMap): Params {
  const kept: Params = {};
  for (const key of query.keys) {
    if (key === PICK_TAB_KEY || key === PICK_VIEW_KEY) continue;
    const values = query.getAll(key);
    kept[key] = values.length === 1 ? values[0] : values;
  }
  return kept;
}

/** Where the Markets list sends a pick: the tab it was asked to open, else Chart. */
export function openableTab(then: string | null | undefined): InstrumentTab {
  return INSTRUMENT_TABS.tabs.find((tab) => tab.id === then)?.id ?? 'chart';
}

function firstString(value: unknown): string | null {
  const first: unknown = Array.isArray(value) ? value[0] : value;
  return typeof first === 'string' && first.trim() !== '' ? first.trim() : null;
}

export interface InstrumentTarget {
  readonly commands: string[];
  readonly queryParams: Params;
}

/**
 * Where an address that named an instrument only by query (`?ticker=SPY`)
 * leads now. Every other key it carried goes along unchanged, the retired
 * `trading-session` under its new name; an address that named no instrument
 * leads to the Markets list, never to a default one.
 */
export function legacyInstrumentTarget(
  queryParams: Params,
  tab: InstrumentTab,
  rest: readonly string[] = [],
): InstrumentTarget {
  const { ticker, symbol, 'trading-session': tradingSession, ...kept } = queryParams;
  const query: Params = { ...kept };
  const session = firstString(tradingSession);
  if (session !== null) query['session'] = session === 'extended' ? 'extended' : 'regular';
  const named = firstString(ticker) ?? firstString(symbol);
  return named === null
    ? { commands: [MARKETS_ROUTE], queryParams: query }
    : { commands: instrumentCommands(named.toUpperCase(), tab, rest), queryParams: query };
}

/** A redirect from an address that names an instrument by query to that instrument, in the one navigation. */
export function redirectToInstrument(tab: InstrumentTab, rest: readonly string[] = []): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const target = legacyInstrumentTarget(queryParams, tab, rest);
    return inject(Router).createUrlTree(target.commands, {
      queryParams: target.queryParams,
      fragment: fragment ?? undefined,
    });
  };
}

/** The tab an instrument address has open, or `null` for any other address or a tab this place does not name. */
export function instrumentTabOf(url: string): InstrumentTab | null {
  if (instrumentSymbolOf(url) === null) return null;
  const tab = routePathOf(url).split('/')[3];
  return INSTRUMENT_TABS.tabs.find((candidate) => candidate.id === tab)?.id ?? null;
}

/**
 * The page of an instrument address below its symbol (`['data', 'build']`), or
 * `null` for any other address or a tab this place's saved setups do not open on.
 */
export function instrumentPageOf(url: string): readonly string[] | null {
  if (instrumentSymbolOf(url) === null) return null;
  const [, , , tab, ...rest] = routePathOf(url).split('/');
  return tab === 'chart' || tab === 'returns' || tab === 'data' ? [tab, ...rest] : null;
}

/** The symbol an address opens, or `null` when the URL is not an instrument address. */
export function instrumentSymbolOf(url: string): string | null {
  const segments = routePathOf(url).split('/');
  return segments[1] === 'markets' && segments.length > 2 && segments[2] !== '' ? segments[2].toUpperCase() : null;
}

/**
 * The window title of an instrument address: the entity first, then its tab
 * and its group, as `SPY · Chart · Markets` (ADR 0084 decision 5). `null` for
 * any other address, so the menu's own title applies.
 */
export function instrumentWindowTitle(url: string): string | null {
  const symbol = instrumentSymbolOf(url);
  if (symbol === null) return null;
  const [, , , tab, sub] = routePathOf(url).split('/');
  const label = sub === HEADLINES_SEGMENT ? 'Headlines' : INSTRUMENT_TABS.tabs.find((candidate) => candidate.id === tab)?.label;
  return label === undefined ? `${symbol} · Markets` : `${symbol} · ${label} · Markets`;
}
