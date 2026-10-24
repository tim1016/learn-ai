import { inject } from '@angular/core';
import { Router, type Params, type RedirectFunction } from '@angular/router';

import { routePathOf } from '../../fleet/account-workspace';
import { PICK_TAB_KEY, PICK_VIEW_KEY, instrumentCommands, instrumentSymbolOf, legacyInstrumentTarget } from '../instrument-address';
import { DEFAULT_OPTIONS_VIEW, OPTIONS_VIEWS, type OptionsView } from './options-views';

/*
 * Where an option view lives (ADR 0084): `/markets/:symbol/options/<view>`,
 * with the expiry it opens on in `?expiry=` (an expiry anchor, `int64 ms UTC`).
 * This file is the one place that spells those addresses and reads an instant
 * out of one: a link to the chain, and the rule for what `?expiry=` and `?at=` hold.
 */

/** The domain's latest instant (end of 9999-12-31 UTC): a longer count of milliseconds is not a time. */
const MAX_INSTANT_MS = 253_402_300_799_999;
const INSTANT = /^\d{1,15}$/;

/**
 * An instant an address carries (`?expiry=`, `?at=`) as `int64 ms UTC`: whole milliseconds, no later than
 * the domain's latest instant. `null` for text that is not one. Nothing is corrected.
 */
export function parseInstantMs(text: string): number | null {
  return INSTANT.test(text) && Number(text) <= MAX_INSTANT_MS ? Number(text) : null;
}

/** Where a link goes: router commands and the query the address carries. */
export interface OptionsLink {
  readonly commands: string[];
  readonly queryParams: Params;
}

/** The instrument's Options chain on Past · Archive, at the session `date` (`YYYY-MM-DD`) when one is named. */
export function archiveChainLink(symbol: string, date: string | null = null): OptionsLink {
  return {
    commands: instrumentCommands(symbol, 'options', ['chain']),
    queryParams: date === null || date === '' ? { source: 'archive' } : { source: 'archive', date },
  };
}

/**
 * The redirect for the address a view had before it moved into the instrument
 * (`/options-lab/chain`, `/broker/options-chain`, `/broker/options-surface`, `/pricing-lab`).
 *
 * A symbol in the query (`?ticker=` or `?symbol=`) leads to that instrument's
 * view in the one navigation, and the expiry and every other key go with it,
 * as does the fragment. An address that names no instrument leads to the
 * Markets list, asked to open this view on the pick (`then=options`, and `view=`
 * for a view other than the default); the list hands the pick every other key
 * the address carried. It never opens a default instrument.
 *
 * `defaults` are keys the old page implied and its address did not spell: the
 * old Chain (Live) page was the IBKR feed, so its address leads to `source=live`.
 * A key the address already carries wins.
 */
export function redirectToOptionsView(view: OptionsView, defaults: Params = {}): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const target = legacyInstrumentTarget(queryParams, 'options', [view]);
    const query: Params = { ...target.queryParams };
    for (const [key, value] of Object.entries(defaults)) if (!(key in query)) query[key] = value;
    // `/markets` alone is what a query with no instrument leads to.
    const namesInstrument = target.commands.length > 1;
    if (!namesInstrument) {
      query[PICK_TAB_KEY] = 'options';
      if (view !== DEFAULT_OPTIONS_VIEW) query[PICK_VIEW_KEY] = view;
    }
    return inject(Router).createUrlTree(target.commands, { queryParams: query, fragment: fragment ?? undefined });
  };
}

/** The view an address names in its own words (`view=pricing`), or `null` for none or one the tab does not route. */
export function namedOptionsView(text: string | null | undefined): OptionsView | null {
  return OPTIONS_VIEWS.tabs.find((candidate) => candidate.id === text)?.id ?? null;
}

/**
 * Router commands for the Options tab of an instrument, on the view an address named. With none, or
 * one the tab does not route, the tab itself, which opens its default view.
 */
export function optionsViewCommands(symbol: string, view: string | null | undefined): string[] {
  const named = namedOptionsView(view);
  return instrumentCommands(symbol, 'options', named === null ? [] : [named]);
}

/** The view an instrument address has open, or `null` for any other address or a view this place does not route. */
export function optionsViewOf(url: string): OptionsView | null {
  if (instrumentSymbolOf(url) === null) return null;
  const [, , , tab, view] = routePathOf(url).split('/');
  return tab === 'options' ? namedOptionsView(view) : null;
}
