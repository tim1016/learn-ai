import type { Routes } from '@angular/router';

import { redirectKeepingState } from '../shell/route-redirects';

/**
 * The `/research` route family (ADR 0084): the study list, one page per study
 * and the Edge demonstrations.
 *
 * Each study is the existing Research Lab runner, now inside the shared study
 * layout (`app-study-page`). A study's header and steps carry their own inset,
 * so the page body adds none.
 */
export const RESEARCH_ROUTES: Routes = [
  {
    path: '',
    pathMatch: 'full',
    data: { fullBleed: true },
    loadComponent: () => import('./study-list/study-list.component').then((m) => m.StudyListComponent),
  },
  {
    path: 'feature',
    data: { fullBleed: true },
    loadComponent: () =>
      import('../components/research-lab/feature-runner/feature-runner.component').then(
        (m) => m.FeatureRunnerComponent,
      ),
  },
  {
    path: 'cross-sectional',
    data: { fullBleed: true },
    loadComponent: () =>
      import('../components/research-lab/batch-runner/batch-runner.component').then((m) => m.BatchRunnerComponent),
  },
  {
    path: 'signal',
    data: { fullBleed: true },
    loadComponent: () =>
      import('../components/research-lab/signal-runner/signal-runner.component').then(
        (m) => m.SignalRunnerComponent,
      ),
  },
  {
    path: 'indicator-reliability',
    data: { fullBleed: true },
    loadComponent: () =>
      import('../components/research-lab/indicator-reliability/indicator-reliability.component').then(
        (m) => m.IndicatorReliabilityComponent,
      ),
  },
  {
    path: 'realized-vs-iv',
    data: { fullBleed: true },
    loadComponent: () =>
      import('../components/edge/realized-vs-iv/realized-vs-iv.component').then((m) => m.RealizedVsIvComponent),
  },
  {
    // Sample data throughout: each page labels what it renders (R28).
    path: 'demos',
    data: { fullBleed: true },
    loadComponent: () => import('./demos/demo-shell.component').then((m) => m.ResearchDemoShellComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: redirectKeepingState(['/research']) },
      {
        path: 'cross-asset',
        loadComponent: () =>
          import('../components/edge/cross-asset/cross-asset.component').then((m) => m.CrossAssetComponent),
      },
      {
        path: 'regimes',
        loadComponent: () =>
          import('../components/edge/regimes/regimes.component').then((m) => m.RegimesComponent),
      },
      {
        path: 'edge-score',
        loadComponent: () => import('./demos/edge-score-demo.component').then((m) => m.EdgeScoreDemoComponent),
      },
      {
        path: 'trade-simulator',
        loadComponent: () =>
          import('./demos/trade-simulator-demo.component').then((m) => m.TradeSimulatorDemoComponent),
      },
    ],
  },
];
