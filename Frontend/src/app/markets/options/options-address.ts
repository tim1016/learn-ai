import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { routePathOf } from '../../fleet/account-workspace';
import { instrumentSymbolOf, legacyInstrumentTarget } from '../instrument-address';
import { OPTIONS_VIEWS, type OptionsView } from './options-views';

/*
 * Where an option view lives (ADR 0084): `/markets/:symbol/options/<view>`,
 * with the expiry it opens on in `?expiry=` (an expiry anchor, `int64 ms UTC`).
 */

/**
 * The redirect for the address a view had before it moved into the instrument
 * (`/broker/options-surface`, `/pricing-lab`).
 *
 * A symbol in the query (`?ticker=` or `?symbol=`) leads to that instrument's
 * view in the one navigation, and the expiry and every other key go with it,
 * as does the fragment. An address that names no instrument leads to the
 * Markets list, asked to open Options on the pick: it never opens a default
 * instrument.
 */
export function redirectToOptionsView(view: OptionsView): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const target = legacyInstrumentTarget(queryParams, 'options', [view]);
    // `/markets` alone is what a query with no instrument leads to.
    const namesInstrument = target.commands.length > 1;
    return inject(Router).createUrlTree(target.commands, {
      queryParams: namesInstrument ? target.queryParams : { ...target.queryParams, then: 'options' },
      fragment: fragment ?? undefined,
    });
  };
}

/** The view an instrument address has open, or `null` for any other address or a view this place does not route. */
export function optionsViewOf(url: string): OptionsView | null {
  if (instrumentSymbolOf(url) === null) return null;
  const [, , , tab, view] = routePathOf(url).split('/');
  return tab === 'options' ? (OPTIONS_VIEWS.tabs.find((candidate) => candidate.id === view)?.id ?? null) : null;
}
