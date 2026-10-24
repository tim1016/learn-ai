import { Routes } from '@angular/router';

import { redirectToInstrument } from '../../markets/instrument-address';
import { dataCheckRoute } from '../../operations/data-checks/data-checks';
import { redirectKeepingState } from '../../shell/route-redirects';

/**
 * Old Research Lab addresses of the five studies, each straight to its study
 * under `/research` (ADR 0084) in one redirect that keeps query and fragment.
 * A relative `redirectTo` cannot chain through a second redirect, so the old
 * `build/*` and `inspect/*` aliases name the final study too.
 */
const movedStudy = (path: string, ...study: [string, ...string[]]): Routes[number] => ({
  path,
  redirectTo: redirectKeepingState(['/research', ...study]),
  pathMatch: 'full',
});

/**
 * An old list address, straight to the Research History filtered to the kind it
 * listed, in one redirect that keeps query and fragment. The older `inspect/*`
 * and tab-id aliases name the final address too, as a relative redirect cannot
 * chain through another.
 */
const movedList = (path: string, type: string): Routes[number] => ({
  path,
  redirectTo: redirectKeepingState(['/research', 'history'], { set: () => ({ type }) }),
  pathMatch: 'full',
});

/**
 * The old address of one spec-run record, straight to its native detail page
 * under `/research/history` (ADR 0084) in one redirect that keeps query and
 * fragment. A spec run and its Monte Carlo, baseline and walk-forward children
 * are Research evidence: the redirect names the record's own kind and id and
 * looks nothing up, so no run is ever re-homed under a strategy that merely
 * resembles its name (PRD #3490 R27).
 */
const movedRecord = (path: string, kind: string, idParam: string): Routes[number] => ({
  path,
  redirectTo: redirectKeepingState((params) => ['/research', 'history', kind, String(params[idParam] ?? '')]),
  pathMatch: 'full',
});

export const researchLabRoutes: Routes = [
  // The spec-run records moved to /research/history.
  movedRecord('strategy-runs/:run_id', 'spec-run', 'run_id'),
  movedRecord('walk-forward/:wf_id', 'walk-forward', 'wf_id'),
  movedRecord('monte-carlo/:mc_id', 'monte-carlo', 'mc_id'),
  movedRecord('baselines/:baseline_id', 'baseline', 'baseline_id'),

  // A stored signal report is a History record: its detail is /research/history/signal-report/:id.
  {
    path: 'signal-report/:id',
    redirectTo: redirectKeepingState((params) => ['/research', 'history', 'signal-report', String(params['id'] ?? '')]),
    pathMatch: 'full',
  },

  // The saved lists moved into the Research History, each filtered to the kind it listed.
  movedList('features/experiments', 'feature-experiment'),
  movedList('inspect/experiments', 'feature-experiment'),
  movedList('experiment-history', 'feature-experiment'),
  movedList('signals/history', 'signal-report'),
  movedList('inspect/signal-history', 'signal-report'),
  movedList('backtests/strategy-runs', 'spec-run'),
  movedList('inspect/strategy-runs', 'spec-run'),

  // The five studies moved to /research.
  movedStudy('features/validate', 'feature'),
  movedStudy('features/cross-sectional', 'cross-sectional'),
  movedStudy('signals/engine', 'signal'),
  movedStudy('backtests/reliability', 'indicator-reliability'),
  movedStudy('build/validate', 'feature'),
  movedStudy('build/indicator-reliability', 'indicator-reliability'),
  movedStudy('build/signal-engine', 'signal'),
  movedStudy('inspect/cross-sectional', 'cross-sectional'),
  movedStudy('feature-runner', 'feature'),
  movedStudy('indicator-reliability', 'indicator-reliability'),
  movedStudy('signal-engine', 'signal'),
  movedStudy('cross-sectional', 'cross-sectional'),

  // The Research Lab shell is gone: its last page, the Recency Chart, moved
  // under Signal strategies, so every address left here is a redirect.
  { path: '', redirectTo: redirectKeepingState(['/research']), pathMatch: 'full' },

  // Market: headlines moved under the instrument (ADR 0084). The old
  // address went straight to them with no symbol in it, so it leads to the
  // instrument its query names, or to Markets when it names none.
  {
    path: 'market/news',
    pathMatch: 'full',
    redirectTo: redirectToInstrument('chart', ['news']),
  },

  // The Recency Chart moved under Signal strategies.
  {
    path: 'backtests/recency-chart',
    redirectTo: redirectKeepingState(['/signal-strategies', 'recency']),
    pathMatch: 'full',
  },

  // Data divergence and Strategy pre-flight moved to Operations → Data checks.
  // Each address goes straight there, carrying its query and fragment.
  { path: 'inspect/data-divergence', redirectTo: redirectKeepingState(dataCheckRoute('divergence')), pathMatch: 'full' },
  { path: 'inspect/pre-flight', redirectTo: redirectKeepingState(dataCheckRoute('pre-flight')), pathMatch: 'full' },

  // Legacy single-page tab IDs (pre-router)
  { path: 'data-divergence', redirectTo: redirectKeepingState(dataCheckRoute('divergence')), pathMatch: 'full' },
  { path: 'strategy-preflight', redirectTo: redirectKeepingState(dataCheckRoute('pre-flight')), pathMatch: 'full' },
];
