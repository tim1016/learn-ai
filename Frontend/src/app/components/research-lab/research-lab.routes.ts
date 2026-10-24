import { Routes } from '@angular/router';

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

  // The signal report keeps focused content while inheriting the root
  // application shell. Listed before the research-lab child shell so its
  // more-specific pattern matches first.
  {
    path: 'signal-report/:id',
    loadComponent: () =>
      import('./signal-report-page/signal-report-page.component').then(
        (m) => m.SignalReportPageComponent,
      ),
  },

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

  // Sub-nav shell for the pages Research Lab still hosts: the Strategy Runs
  // list and History (#3519), instrument headlines, the Recency Chart and the
  // two data checks (the slices of the places they belong to).
  {
    path: '',
    loadComponent: () =>
      import('./research-lab.component').then((m) => m.ResearchLabComponent),
    children: [
      { path: '', redirectTo: redirectKeepingState(['/research']), pathMatch: 'full' },

      {
        path: 'features/experiments',
        loadComponent: () =>
          import('./experiment-history/experiment-history.component').then(
            (m) => m.ExperimentHistoryComponent,
          ),
        data: { title: 'Experiment History' },
      },

      {
        path: 'signals/history',
        loadComponent: () =>
          import('./signal-history/signal-history.component').then(
            (m) => m.SignalHistoryComponent,
          ),
        data: { title: 'Signal History' },
      },

      // Market
      {
        path: 'market/news',
        loadComponent: () =>
          import('./news/news-page.component').then((m) => m.NewsPageComponent),
        data: { title: 'Ticker News' },
      },

      // Backtests
      {
        path: 'backtests/strategy-runs',
        loadComponent: () =>
          import('./strategy-runs/strategy-runs.component').then(
            (m) => m.StrategyRunsComponent,
          ),
        data: { title: 'Backtest Runs' },
      },
      {
        path: 'backtests/recency-chart',
        loadComponent: () =>
          import(
            './recency-chart/recency-chart-page.component'
          ).then((m) => m.RecencyChartPageComponent),
        data: { title: 'Recency Chart' },
      },

      // Nav-invisible routes (no longer surfaced in sub-nav but still reachable)
      {
        path: 'inspect/data-divergence',
        loadComponent: () =>
          import('./data-divergence/data-divergence.component').then(
            (m) => m.DataDivergenceComponent,
          ),
        data: { title: 'Data Divergence' },
      },
      {
        path: 'inspect/pre-flight',
        loadComponent: () =>
          import('./strategy-preflight/strategy-preflight.component').then(
            (m) => m.StrategyPreflightComponent,
          ),
        data: { title: 'Strategy Pre-flight' },
      },

      // Legacy redirects — old build/* and inspect/* paths → new canonical paths
      { path: 'inspect/strategy-runs', redirectTo: 'backtests/strategy-runs', pathMatch: 'full' },
      { path: 'inspect/experiments', redirectTo: 'features/experiments', pathMatch: 'full' },
      { path: 'inspect/signal-history', redirectTo: 'signals/history', pathMatch: 'full' },

      // Legacy single-page tab IDs (pre-router)
      { path: 'data-divergence', redirectTo: 'inspect/data-divergence', pathMatch: 'full' },
      { path: 'strategy-preflight', redirectTo: 'inspect/pre-flight', pathMatch: 'full' },
      { path: 'experiment-history', redirectTo: 'features/experiments', pathMatch: 'full' },
    ],
  },
];
