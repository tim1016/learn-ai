import { inject } from '@angular/core';
import { Router, type RedirectFunction } from '@angular/router';

import { legacyInstrumentTarget } from '../../markets/instrument-address';
import type { InstrumentTab } from '../../markets/instrument-tabs';
import { dataCheckRoute } from '../../operations/data-checks/data-checks';
import { operationsTabRoute, type OperationsTab } from '../../operations/operations-tabs';
import { redirectKeepingState } from '../../shell/route-redirects';

/* Legacy Data Lab addresses.
 *
 * The Data Lab is gone: its chart, Returns study, dataset builder and saved
 * setups live under the instrument they are for, and its Validate page and
 * option-archive card live in Operations (ADR 0084). What is left of
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

/** Where a legacy address ends in the instrument place. */
export type LegacyDataLabTarget = 'chart' | 'returns' | 'build';

/** Where each target lives under `/markets/:symbol`. */
const INSTRUMENT_PAGE: Readonly<Record<LegacyDataLabTarget, { tab: InstrumentTab; rest: readonly string[] }>> = {
  chart: { tab: 'chart', rest: [] },
  returns: { tab: 'returns', rest: [] },
  build: { tab: 'data', rest: ['build'] },
};

/** What a legacy `mode` names: an instrument page, or Validate, which is a check in Operations now. */
const MODE_TO_TARGET: Readonly<Record<string, LegacyDataLabTarget | 'validate'>> = {
  explore: 'chart',
  build: 'build',
  export: 'build',
  validate: 'validate',
};

function targetForMode(mode: unknown): LegacyDataLabTarget | 'validate' | undefined {
  return typeof mode === 'string' && Object.hasOwn(MODE_TO_TARGET, mode) ? MODE_TO_TARGET[mode] : undefined;
}

/**
 * The Operations tab a retired archive-card anchor belongs to. The card sat on
 * top of every Data Lab page with one anchor per underlying
 * (`#option-archive-SPY`) and one each for the lake backup and the weekly
 * restore check; those moved to Operations, so an old link carrying one goes
 * there.
 */
function archiveTabFor(fragment: string | null | undefined): OperationsTab | null {
  if (typeof fragment !== 'string' || !/^option-archive(-|$)/.test(fragment)) return null;
  return fragment === 'option-archive-backup' || fragment === 'option-archive-restore' ? 'backups' : 'option-archive';
}

/**
 * The redirect for a legacy Data Lab address (`/data-lab`, `/data-lab/explore`,
 * `/data-lab/export`, `/data-lab/returns`).
 *
 * An archive-card anchor leaves for the Operations tab that holds that part of
 * the card, query and fragment intact. Otherwise `mode` decides the page when it
 * names one, else `fallback` does; a `mode` that names a page is spent, because
 * the path now says it. A `mode` that names none stays in the query, where the
 * instrument place names it as ignored.
 */
export function redirectLegacyDataLab(fallback: LegacyDataLabTarget): RedirectFunction {
  return (source) => {
    const named = targetForMode(source.queryParams['mode']);
    const drop = named === undefined ? [] : ['mode'];
    const archiveTab = archiveTabFor(source.fragment);
    if (archiveTab !== null) return redirectKeepingState(operationsTabRoute(archiveTab), { drop })(source);
    if (named === 'validate') return redirectKeepingState(dataCheckRoute('tradingview'), { drop })(source);

    const page = INSTRUMENT_PAGE[named ?? fallback];
    const kept = Object.fromEntries(Object.entries(source.queryParams).filter(([key]) => !drop.includes(key)));
    const instrument = legacyInstrumentTarget(kept, page.tab, page.rest);
    return inject(Router).createUrlTree(instrument.commands, {
      queryParams: instrument.queryParams,
      fragment: source.fragment ?? undefined,
    });
  };
}
