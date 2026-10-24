import { Routes } from '@angular/router';

import { redirectToInstrument } from '../../markets/instrument-address';
import { dataCheckRoute } from '../../operations/data-checks/data-checks';
import { redirectKeepingState } from '../../shell/route-redirects';

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

      // Market: headlines moved under the instrument (ADR 0084). The old
      // address went straight to them with no symbol in it, so it leads to the
      // instrument its query names, or to Markets when it names none.
      {
        path: 'market/news',
        pathMatch: 'full',
        redirectTo: redirectToInstrument('chart', ['news']),
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
      // The multi-strategy comparison moved under the Signal strategies list.
      {
        path: 'backtests/recency-chart',
        redirectTo: redirectKeepingState(['/signal-strategies', 'recency']),
        pathMatch: 'full',
      },

      // Data divergence and Strategy pre-flight moved to Operations → Data checks.
      // Each address goes straight there, carrying its query and fragment.
      { path: 'inspect/data-divergence', redirectTo: redirectKeepingState(dataCheckRoute('divergence')), pathMatch: 'full' },
      { path: 'inspect/pre-flight', redirectTo: redirectKeepingState(dataCheckRoute('pre-flight')), pathMatch: 'full' },

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
      { path: 'data-divergence', redirectTo: redirectKeepingState(dataCheckRoute('divergence')), pathMatch: 'full' },
      { path: 'strategy-preflight', redirectTo: redirectKeepingState(dataCheckRoute('pre-flight')), pathMatch: 'full' },
      { path: 'experiment-history', redirectTo: 'features/experiments', pathMatch: 'full' },
    ],
  },
];
