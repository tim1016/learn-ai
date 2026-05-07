import { Routes } from '@angular/router';

const BUILD_SUBTITLES = {
  validate:
    "Test a candidate feature's predictive power via Information Coefficient against forward log returns.",
  reliability:
    "Score an indicator's stability, parameter sensitivity, and regime robustness.",
  signalEngine:
    'Compose features into signals and validate them against the same IC bar.',
} as const;

const INSPECT_SUBTITLES = {
  crossSectional:
    'Run one feature across the full universe and rank by IC.',
  dataDivergence:
    'Audit divergence between vendor data feeds before trusting downstream math.',
  preFlight:
    'Validate strategy config and data coverage before queuing a backtest run.',
  strategyRuns:
    'All completed strategy backtests with walk-forward and Monte Carlo drilldowns.',
  experiments:
    'Audit log of feature validation runs.',
  signalHistory:
    'Audit log of signal engine runs.',
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
      { path: '', redirectTo: 'build/validate', pathMatch: 'full' },

      // Build
      {
        path: 'build/validate',
        loadComponent: () =>
          import('./feature-runner/feature-runner.component').then(
            (m) => m.FeatureRunnerComponent,
          ),
        data: { title: 'Feature Validation', subtitle: BUILD_SUBTITLES.validate },
      },
      {
        path: 'build/indicator-reliability',
        loadComponent: () =>
          import(
            './indicator-reliability/indicator-reliability.component'
          ).then((m) => m.IndicatorReliabilityComponent),
        data: {
          title: 'Indicator Reliability',
          subtitle: BUILD_SUBTITLES.reliability,
        },
      },
      {
        path: 'build/signal-engine',
        loadComponent: () =>
          import('./signal-runner/signal-runner.component').then(
            (m) => m.SignalRunnerComponent,
          ),
        data: { title: 'Signal Engine', subtitle: BUILD_SUBTITLES.signalEngine },
      },

      // Inspect
      {
        path: 'inspect/cross-sectional',
        loadComponent: () =>
          import('./batch-runner/batch-runner.component').then(
            (m) => m.BatchRunnerComponent,
          ),
        data: {
          title: 'Cross-Sectional Sweep',
          subtitle: INSPECT_SUBTITLES.crossSectional,
        },
      },
      {
        path: 'inspect/data-divergence',
        loadComponent: () =>
          import('./data-divergence/data-divergence.component').then(
            (m) => m.DataDivergenceComponent,
          ),
        data: {
          title: 'Data Divergence',
          subtitle: INSPECT_SUBTITLES.dataDivergence,
        },
      },
      {
        path: 'inspect/pre-flight',
        loadComponent: () =>
          import('./strategy-preflight/strategy-preflight.component').then(
            (m) => m.StrategyPreflightComponent,
          ),
        data: {
          title: 'Strategy Pre-flight',
          subtitle: INSPECT_SUBTITLES.preFlight,
        },
      },
      {
        path: 'inspect/strategy-runs',
        loadComponent: () =>
          import('./strategy-runs/strategy-runs.component').then(
            (m) => m.StrategyRunsComponent,
          ),
        data: {
          title: 'Backtest Runs',
          subtitle: INSPECT_SUBTITLES.strategyRuns,
        },
      },
      {
        path: 'inspect/experiments',
        loadComponent: () =>
          import('./experiment-history/experiment-history.component').then(
            (m) => m.ExperimentHistoryComponent,
          ),
        data: {
          title: 'Experiment History',
          subtitle: INSPECT_SUBTITLES.experiments,
        },
      },
      {
        path: 'inspect/signal-history',
        loadComponent: () =>
          import('./signal-history/signal-history.component').then(
            (m) => m.SignalHistoryComponent,
          ),
        data: {
          title: 'Signal History',
          subtitle: INSPECT_SUBTITLES.signalHistory,
        },
      },

      // Legacy in-page tab IDs → redirects so any external links / bookmarks
      // formed against the old single-page switch keep resolving.
      { path: 'feature-runner', redirectTo: 'build/validate', pathMatch: 'full' },
      {
        path: 'indicator-reliability',
        redirectTo: 'build/indicator-reliability',
        pathMatch: 'full',
      },
      {
        path: 'signal-engine',
        redirectTo: 'build/signal-engine',
        pathMatch: 'full',
      },
      {
        path: 'cross-sectional',
        redirectTo: 'inspect/cross-sectional',
        pathMatch: 'full',
      },
      {
        path: 'data-divergence',
        redirectTo: 'inspect/data-divergence',
        pathMatch: 'full',
      },
      {
        path: 'strategy-preflight',
        redirectTo: 'inspect/pre-flight',
        pathMatch: 'full',
      },
      {
        path: 'experiment-history',
        redirectTo: 'inspect/experiments',
        pathMatch: 'full',
      },
    ],
  },
];
