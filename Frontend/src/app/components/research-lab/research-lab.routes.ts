import { Routes } from '@angular/router';

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

  // Shell with two-group sub-nav and lazy children.
  {
    path: '',
    loadComponent: () =>
      import('./research-lab.component').then((m) => m.ResearchLabComponent),
    children: [
      { path: '', redirectTo: 'features/validate', pathMatch: 'full' },

      // Features
      {
        path: 'features/validate',
        loadComponent: () =>
          import('./feature-runner/feature-runner.component').then(
            (m) => m.FeatureRunnerComponent,
          ),
        data: { title: 'Feature Validation' },
      },
      {
        path: 'features/cross-sectional',
        loadComponent: () =>
          import('./batch-runner/batch-runner.component').then(
            (m) => m.BatchRunnerComponent,
          ),
        data: { title: 'Cross-Sectional Sweep' },
      },
      {
        path: 'features/experiments',
        loadComponent: () =>
          import('./experiment-history/experiment-history.component').then(
            (m) => m.ExperimentHistoryComponent,
          ),
        data: { title: 'Experiment History' },
      },

      // Signals
      {
        path: 'signals/engine',
        loadComponent: () =>
          import('./signal-runner/signal-runner.component').then(
            (m) => m.SignalRunnerComponent,
          ),
        data: { title: 'Signal Engine' },
      },
      {
        path: 'signals/history',
        loadComponent: () =>
          import('./signal-history/signal-history.component').then(
            (m) => m.SignalHistoryComponent,
          ),
        data: { title: 'Signal History' },
      },

      // Backtests
      {
        path: 'backtests/reliability',
        loadComponent: () =>
          import(
            './indicator-reliability/indicator-reliability.component'
          ).then((m) => m.IndicatorReliabilityComponent),
        data: { title: 'Indicator Reliability' },
      },
      {
        path: 'backtests/strategy-runs',
        loadComponent: () =>
          import('./strategy-runs/strategy-runs.component').then(
            (m) => m.StrategyRunsComponent,
          ),
        data: { title: 'Backtest Runs' },
      },
      {
        path: 'backtests/spy-ema-walk-forward',
        loadComponent: () =>
          import(
            './spy-ema-walk-forward/spy-ema-walk-forward-page.component'
          ).then((m) => m.SpyEmaWalkForwardPageComponent),
        data: { title: 'SPY EMA Walk-Forward' },
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
      { path: 'build/validate', redirectTo: 'features/validate', pathMatch: 'full' },
      { path: 'build/indicator-reliability', redirectTo: 'backtests/reliability', pathMatch: 'full' },
      { path: 'build/signal-engine', redirectTo: 'signals/engine', pathMatch: 'full' },
      { path: 'inspect/cross-sectional', redirectTo: 'features/cross-sectional', pathMatch: 'full' },
      { path: 'inspect/strategy-runs', redirectTo: 'backtests/strategy-runs', pathMatch: 'full' },
      { path: 'inspect/experiments', redirectTo: 'features/experiments', pathMatch: 'full' },
      { path: 'inspect/signal-history', redirectTo: 'signals/history', pathMatch: 'full' },

      // Legacy single-page tab IDs (pre-router)
      { path: 'feature-runner', redirectTo: 'features/validate', pathMatch: 'full' },
      { path: 'indicator-reliability', redirectTo: 'backtests/reliability', pathMatch: 'full' },
      { path: 'signal-engine', redirectTo: 'signals/engine', pathMatch: 'full' },
      { path: 'cross-sectional', redirectTo: 'features/cross-sectional', pathMatch: 'full' },
      { path: 'data-divergence', redirectTo: 'inspect/data-divergence', pathMatch: 'full' },
      { path: 'strategy-preflight', redirectTo: 'inspect/pre-flight', pathMatch: 'full' },
      { path: 'experiment-history', redirectTo: 'features/experiments', pathMatch: 'full' },
    ],
  },
];
