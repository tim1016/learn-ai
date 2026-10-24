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

  // Sub-nav shell for the pages Research Lab still hosts: Strategy Runs and
  // History (#3518, #3519), instrument headlines, the Recency Chart and the two
  // data checks (the slices of the places they belong to).
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
