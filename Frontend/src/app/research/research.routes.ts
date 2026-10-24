import type { Route, Routes } from '@angular/router';

import { redirectKeepingState } from '../shell/route-redirects';
import { STORED_STUDY_TYPE_IDS } from './stored-study/stored-study';

/**
 * The `/research` route family (ADR 0084): the study list, one page per study,
 * the Rule sandbox, the Edge demonstrations, and the History of every study's saved
 * results with the native detail page of each spec-run record.
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
    // Not a study: free-form rules replayed on past prices, never deployable (R27).
    path: 'rule-sandbox',
    data: { fullBleed: true },
    loadComponent: () =>
      import('../components/spec-strategy-runner/spec-strategy-runner.component').then(
        (m) => m.SpecStrategyRunnerComponent,
      ),
  },
  {
    // The History: one typed index over every store that keeps research results (R26).
    path: 'history',
    data: { fullBleed: true },
    loadComponent: () =>
      import('./history/research-history-page.component').then((m) => m.ResearchHistoryPageComponent),
  },
  // The detail of a History entry of one of the five studies' own runs: what was saved, read and never rerun.
  ...STORED_STUDY_TYPE_IDS.map(
    (study): Route => ({
      path: `history/${study}/:id`,
      data: { fullBleed: true, study },
      loadComponent: () =>
        import('./stored-study/stored-study-page.component').then((m) => m.StoredStudyPageComponent),
    }),
  ),
  {
    // The detail of a History entry of the retired store's signal reports: the stored report, read and never rerun.
    path: 'history/signal-report/:id',
    loadComponent: () =>
      import('../components/research-lab/signal-report-page/signal-report-page.component').then(
        (m) => m.SignalReportPageComponent,
      ),
  },
  // A `StrategySpec` run and its Monte Carlo, benchmark-baseline and
  // walk-forward children stay Research evidence at their native detail pages.
  // Each is keyed by its own id: a run is never looked up by a name that
  // resembles a registered strategy's (R27).
  {
    path: 'history/spec-run/:run_id',
    loadComponent: () =>
      import('../components/research-lab/strategy-runs/run-detail-page/run-detail-page.component').then(
        (m) => m.RunDetailPageComponent,
      ),
  },
  {
    path: 'history/walk-forward/:wf_id',
    loadComponent: () =>
      import('../components/research-lab/walk-forward/walk-forward-detail-page/walk-forward-detail-page.component').then(
        (m) => m.WalkForwardDetailPageComponent,
      ),
  },
  {
    path: 'history/monte-carlo/:mc_id',
    loadComponent: () =>
      import('../components/research-lab/monte-carlo/monte-carlo-detail-page/monte-carlo-detail-page.component').then(
        (m) => m.MonteCarloDetailPageComponent,
      ),
  },
  {
    path: 'history/baseline/:baseline_id',
    loadComponent: () =>
      import('../components/research-lab/baselines/baselines-detail-page/baselines-detail-page.component').then(
        (m) => m.BaselinesDetailPageComponent,
      ),
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
