import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { legacyInstrumentTarget } from '../../markets/instrument-address';
import type { InstrumentTab } from '../../markets/instrument-tabs';

/* Legacy Data Lab addresses.
 *
 * The Data Lab is gone: its chart, Returns study, dataset builder and saved
 * setups live under the instrument they are for (ADR 0084). What is left of
 * `/data-lab` is the addresses people saved, and `redirectLegacyDataLab` sends
 * each straight to where its page went, in the navigation the router is
 * already making: the instrument its `ticker` names with its window and
 * `trading-session` (now `session`), or the Markets list when it names none. It
 * never opens a default instrument.
 *
 * Everything else the bookmark carried goes with it — the query and the
 * fragment — so nothing is dropped unsaid: a key the instrument's tabs do not
 * read is named by the place in its "Migrated link" notice, and a `sessionId`
 * opens that saved setup under its own instrument. */

/**
 * The one page still under `/data-lab`, which the option-archive card sits
 * above, until Operations → Option archive and Data checks take both (#3498,
 * #3499). A legacy archive anchor ends here.
 */
const VALIDATE = '/data-lab/validate' as const;

/** Where a legacy address ends: a page of the instrument place, or the one page still under `/data-lab`. */
export type LegacyDataLabTarget = 'chart' | 'returns' | 'build' | typeof VALIDATE;

/** Where each instrument-bound target lives under `/markets/:symbol`. */
const INSTRUMENT_PAGE: Readonly<Record<'chart' | 'returns' | 'build', { tab: InstrumentTab; rest: readonly string[] }>> = {
  chart: { tab: 'chart', rest: [] },
  returns: { tab: 'returns', rest: [] },
  build: { tab: 'data', rest: ['build'] },
};

const MODE_TO_TARGET: Readonly<Record<string, LegacyDataLabTarget>> = {
  explore: 'chart',
  build: 'build',
  export: 'build',
  validate: VALIDATE,
};

/** The page a legacy `mode` names, or `undefined` for a value that names none. */
function targetForMode(mode: unknown): LegacyDataLabTarget | undefined {
  return typeof mode === 'string' && Object.hasOwn(MODE_TO_TARGET, mode) ? MODE_TO_TARGET[mode] : undefined;
}

/** An anchor on the option-archive card (`#option-archive-SPY`, `#option-archive-backup`). */
function isArchiveAnchor(fragment: string | null | undefined): boolean {
  return typeof fragment === 'string' && /^option-archive(-|$)/.test(fragment);
}

/**
 * The redirect for a legacy Data Lab address (`/data-lab`, `/data-lab/explore`,
 * `/data-lab/export`, `/data-lab/returns`, `/data-quality`).
 *
 * `mode` decides the page when it names one, else `fallback` does; a `mode`
 * that names a page is spent, because the path now says it. A `mode` that names
 * none stays in the query, where the instrument place names it as ignored.
 * An archive-card anchor leads to the page the card sits above.
 */
export function redirectLegacyDataLab(fallback: LegacyDataLabTarget): RedirectFunction {
  return ({ queryParams, fragment }) => {
    const named = targetForMode(queryParams['mode']);
    const { mode: _spent, ...rest } = queryParams;
    const kept = named === undefined ? queryParams : rest;
    const target = isArchiveAnchor(fragment) ? VALIDATE : (named ?? fallback);
    const router = inject(Router);
    if (target === VALIDATE) return router.createUrlTree([VALIDATE], { queryParams: kept, fragment: fragment ?? undefined });
    const page = INSTRUMENT_PAGE[target];
    const instrument = legacyInstrumentTarget(kept, page.tab, page.rest);
    return router.createUrlTree(instrument.commands, {
      queryParams: instrument.queryParams,
      fragment: fragment ?? undefined,
    });
  };
}
