import { Routes } from '@angular/router';

const SUBTITLES = {
  validate:
    "Test a candidate feature's predictive power via Information Coefficient against forward log returns.",
  crossSectional:
    'Run one feature across the full universe and rank by IC.',
  experiments:
    'Audit log of feature validation runs.',
  signalEngine:
    'Compose features into signals and validate them against the same IC bar.',
  signalHistory:
    'Audit log of signal engine runs.',
  reliability:
    "Score an indicator's stability, parameter sensitivity, and regime robustness.",
  strategyRuns:
    'All completed strategy backtests with walk-forward and Monte Carlo drilldowns.',
  dataDivergence:
    'Audit divergence between vendor data feeds before trusting downstream math.',
  preFlight:
    'Validate strategy config and data coverage before queuing a backtest run.',
} as const;

export const researchLabRoutes: Routes = [
  // Detail routes — full-screen, no shell chrome. Listed before the shell so
  // their more-specific patterns match first.
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
        data: { title: 'Feature Validation', subtitle: SUBTITLES.validate },
      },
      {
        path: 'features/cross-sectional',
        loadComponent: () =>
          import('./batch-runner/batch-runner.component').then(
            (m) => m.BatchRunnerComponent,
          ),
        data: { title: 'Cross-Sectional Sweep', subtitle: SUBTITLES.crossSectional },
      },
      {
        path: 'features/experiments',
        loadComponent: () =>
          import('./experiment-history/experiment-history.component').then(
            (m) => m.ExperimentHistoryComponent,
          ),
        data: { title: 'Experiment History', subtitle: SUBTITLES.experiments },
      },

      // Signals
      {
        path: 'signals/engine',
        loadComponent: () =>
          import('./signal-runner/signal-runner.component').then(
            (m) => m.SignalRunnerComponent,
          ),
        data: { title: 'Signal Engine', subtitle: SUBTITLES.signalEngine },
      },
      {
        path: 'signals/history',
        loadComponent: () =>
          import('./signal-history/signal-history.component').then(
            (m) => m.SignalHistoryComponent,
          ),
        data: { title: 'Signal History', subtitle: SUBTITLES.signalHistory },
      },

      // Backtests
      {
        path: 'backtests/reliability',
        loadComponent: () =>
          import(
            './indicator-reliability/indicator-reliability.component'
          ).then((m) => m.IndicatorReliabilityComponent),
        data: { title: 'Indicator Reliability', subtitle: SUBTITLES.reliability },
      },
      {
        path: 'backtests/strategy-runs',
        loadComponent: () =>
          import('./strategy-runs/strategy-runs.component').then(
            (m) => m.StrategyRunsComponent,
          ),
        data: { title: 'Backtest Runs', subtitle: SUBTITLES.strategyRuns },
      },

      // Nav-invisible routes (no longer surfaced in sub-nav but still reachable)
      {
        path: 'inspect/data-divergence',
        loadComponent: () =>
          import('./data-divergence/data-divergence.component').then(
            (m) => m.DataDivergenceComponent,
          ),
        data: { title: 'Data Divergence', subtitle: SUBTITLES.dataDivergence },
      },
      {
        path: 'inspect/pre-flight',
        loadComponent: () =>
          import('./strategy-preflight/strategy-preflight.component').then(
            (m) => m.StrategyPreflightComponent,
          ),
        data: { title: 'Strategy Pre-flight', subtitle: SUBTITLES.preFlight },
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
