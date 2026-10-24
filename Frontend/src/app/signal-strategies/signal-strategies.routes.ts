import type { CanMatchFn, Routes } from '@angular/router';

import { lazyEntryResolver } from './explore/entry/explore-entry.lazy';
import { lazyGoldenEntryResolver } from './golden/entry/golden-entry.lazy';
import { canBeStrategyKey } from './signal-strategy-tabs';

/**
 * A strategy's address is its key and a tab, so a lone segment or a reserved one
 * (`evidence`) is not one, and the address is left to the not-found page.
 */
const strategyAddress: CanMatchFn = (_route, segments) =>
  segments.length >= 2 && canBeStrategyKey(segments[0].path);

/**
 * The Signal strategies place (ADR 0084): the registry's list, the multi-strategy
 * Recency Chart comparison, and one strategy's workspace by registry key.
 *
 * A strategy's address always carries its tab, so a bare `/signal-strategies/:key`
 * names nothing and ends at the not-found page, and `recency` is never taken for
 * a key. Explore hosts Backtest, Sweep and Walk-forward, each reading its
 * own native `?run=` or `?search=`. Golden holds three homes, each for one kind of
 * evidence (ADR 0085): Studies (and one study), Versions and Configuration review,
 * and it is the only place settings are approved. Proof is the program's proof and
 * its flag. Golden opens an unlocked plan from Explore evidence (`?run=`,
 * `?search=`, `?walkForward=`); nothing in it starts on its own.
 *
 * Evidence whose owner is not in its address (`evidence/run/:id`,
 * `evidence/search/:id`, `evidence/study/:id`) resolves its strategy from the
 * record itself, lands on that strategy's page in one redirect, and fails in
 * place, keeping the id, when the record names none the registry lists. A saved
 * run is the exception: one no registered strategy holds (retired, renamed, or a
 * custom LEAN source) is shown by itself, read-only, at its evidence address.
 */
export const SIGNAL_STRATEGIES_ROUTES: Routes = [
  {
    path: '',
    pathMatch: 'full',
    loadComponent: () =>
      import('./signal-strategies-list-page.component').then((m) => m.SignalStrategiesListPageComponent),
  },
  {
    path: 'recency',
    loadComponent: () =>
      import('./recency-chart/recency-chart-page.component').then((m) => m.RecencyChartPageComponent),
  },
  {
    path: 'evidence/run/:id',
    resolve: { entry: lazyEntryResolver((resolvers) => resolvers.resolveEvidenceRunEntry) },
    loadComponent: () =>
      import('./explore/entry/explore-entry.component').then((m) => m.ExploreEntryComponent),
  },
  {
    path: 'evidence/search/:id',
    resolve: { entry: lazyEntryResolver((resolvers) => resolvers.resolveSweepEntry) },
    loadComponent: () =>
      import('./explore/entry/explore-entry.component').then((m) => m.ExploreEntryComponent),
  },
  {
    path: 'evidence/study/:id',
    resolve: { entry: lazyGoldenEntryResolver((resolvers) => resolvers.resolveGoldenStudiesEntry) },
    loadComponent: () => import('./golden/entry/golden-entry.component').then((m) => m.GoldenEntryComponent),
  },
  {
    path: ':key',
    canMatch: [strategyAddress],
    loadComponent: () =>
      import('./signal-strategy-workspace.component').then((m) => m.SignalStrategyWorkspaceComponent),
    children: [
      {
        path: 'overview',
        loadComponent: () =>
          import('./overview/signal-strategy-overview.component').then((m) => m.SignalStrategyOverviewComponent),
      },
      {
        path: 'define',
        loadComponent: () =>
          import('./define/signal-strategy-define.component').then((m) => m.SignalStrategyDefineComponent),
      },
      {
        // The tools share the strategy's Explore scope, which the workspace holds above them.
        path: 'explore',
        loadComponent: () => import('./explore/explore.component').then((m) => m.ExploreComponent),
        children: [
          { path: '', pathMatch: 'full', redirectTo: 'backtest' },
          {
            path: 'backtest',
            loadComponent: () =>
              import('./explore/backtest/explore-backtest.component').then((m) => m.ExploreBacktestComponent),
          },
          {
            path: 'sweep',
            loadComponent: () =>
              import('./explore/sweep/explore-sweep.component').then((m) => m.ExploreSweepComponent),
          },
          {
            path: 'walk-forward',
            loadComponent: () =>
              import('./explore/walk-forward/explore-walk-forward.component').then((m) => m.ExploreWalkForwardComponent),
          },
        ],
      },
      {
        // The one place settings are approved. Its pages share the strategy's Explore scope too,
        // so an Explore handoff opens a plan on the same declarations without reading them again.
        path: 'golden',
        loadComponent: () => import('./golden/signal-strategy-golden.component').then((m) => m.SignalStrategyGoldenComponent),
        children: [
          { path: '', pathMatch: 'full', redirectTo: 'studies' },
          {
            path: 'studies',
            loadComponent: () => import('./golden/golden-studies.component').then((m) => m.GoldenStudiesComponent),
          },
          {
            path: 'studies/:studyId',
            loadComponent: () => import('./golden/golden-study.component').then((m) => m.GoldenStudyComponent),
          },
          {
            path: 'versions',
            loadComponent: () => import('./golden/golden-versions.component').then((m) => m.GoldenVersionsComponent),
          },
          {
            path: 'configuration',
            loadComponent: () => import('./golden/golden-configuration.component').then((m) => m.GoldenConfigurationComponent),
          },
        ],
      },
      {
        path: 'proof',
        loadComponent: () => import('./proof/signal-strategy-proof.component').then((m) => m.SignalStrategyProofComponent),
      },
    ],
  },
];
