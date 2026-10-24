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

export const researchLabRoutes: Routes = [
  // Detail routes retain focused content while inheriting the root application
  // shell. Listed before the research-lab child shell so their more-specific
  // patterns match first.
  {
    path: 'strategy-runs/:run_id',
    loadComponent: () =>
      import('./strategy-runs/run-detail-page/run-detail-page.component').then(
        (m) => m.RunDetailPageComponent,
      ),
  },
  {
    path: 'walk-forward/:wf_id',
    loadComponent: () =>
      import(
        './walk-forward/walk-forward-detail-page/walk-forward-detail-page.component'
      ).then((m) => m.WalkForwardDetailPageComponent),
  },
  {
    path: 'monte-carlo/:mc_id',
    loadComponent: () =>
      import(
        './monte-carlo/monte-carlo-detail-page/monte-carlo-detail-page.component'
      ).then((m) => m.MonteCarloDetailPageComponent),
  },
  {
    path: 'baselines/:baseline_id',
    loadComponent: () =>
      import(
        './baselines/baselines-detail-page/baselines-detail-page.component'
      ).then((m) => m.BaselinesDetailPageComponent),
  },
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

  // Sub-nav shell for the pages Research Lab still hosts: the run detail pages
  // (#3518), instrument headlines, the Recency Chart and the two data checks
  // (the slices of the places they belong to).
  {
    path: '',
    loadComponent: () =>
      import('./research-lab.component').then((m) => m.ResearchLabComponent),
    children: [
      { path: '', redirectTo: redirectKeepingState(['/research']), pathMatch: 'full' },

      // Market
      {
        path: 'market/news',
        loadComponent: () =>
          import('./news/news-page.component').then((m) => m.NewsPageComponent),
        data: { title: 'Ticker News' },
      },

      // Backtests
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

      // Legacy single-page tab IDs (pre-router)
      { path: 'data-divergence', redirectTo: 'inspect/data-divergence', pathMatch: 'full' },
      { path: 'strategy-preflight', redirectTo: 'inspect/pre-flight', pathMatch: 'full' },
    ],
  },
];
