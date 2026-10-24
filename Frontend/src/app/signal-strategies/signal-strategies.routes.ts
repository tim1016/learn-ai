import type { CanMatchFn, Routes } from '@angular/router';

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
 * a key. Explore, Golden and Proof are declared in
 * `signal-strategy-tabs.ts` and have no page yet: the strip shows them
 * unavailable, with why, until each is routed here.
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
    ],
  },
];
