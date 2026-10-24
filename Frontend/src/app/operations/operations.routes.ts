import type { Routes } from '@angular/router';

import { redirectKeepingState } from '../shell/route-redirects';
import { dataCheckRoute } from './data-checks/data-checks';
import { operationsTabRoute } from './operations-tabs';

/**
 * The `/operations` route family (ADR 0084): the Operations header over one
 * child per tab. The tab ids are the URL segments declared in
 * `operations-tabs.ts`. Data checks is a second level: one child per check,
 * declared in `data-checks/data-checks.ts`.
 */
export const OPERATIONS_ROUTES: Routes = [
  {
    path: '',
    // The header and tab strip carry their own inset, so the page body adds none;
    // the workspace gives each tab the same inset.
    data: { fullBleed: true },
    loadComponent: () =>
      import('./operations-workspace.component').then((m) => m.OperationsWorkspaceComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: redirectKeepingState(operationsTabRoute('lake')) },
      {
        path: 'lake',
        loadComponent: () => import('./lake/lake-tab.component').then((m) => m.LakeTabComponent),
      },
      {
        path: 'option-archive',
        loadComponent: () =>
          import('./option-archive/option-archive-tab.component').then((m) => m.OptionArchiveTabComponent),
      },
      {
        path: 'backups',
        loadComponent: () => import('./backups/backups-tab.component').then((m) => m.BackupsTabComponent),
      },
      {
        path: 'math-checks',
        loadComponent: () =>
          import('./math-checks/math-checks-tab.component').then((m) => m.MathChecksTabComponent),
      },
      {
        path: 'data-checks',
        loadComponent: () =>
          import('./data-checks/data-checks-tab.component').then((m) => m.DataChecksTabComponent),
        children: [
          { path: '', pathMatch: 'full', redirectTo: redirectKeepingState(dataCheckRoute('tradingview')) },
          {
            path: 'tradingview',
            loadComponent: () =>
              import('./data-checks/tradingview/tradingview-check.component').then((m) => m.TradingviewCheckComponent),
          },
          {
            path: 'divergence',
            loadComponent: () =>
              import('./data-checks/divergence/data-divergence.component').then((m) => m.DataDivergenceComponent),
          },
          {
            path: 'pre-flight',
            loadComponent: () =>
              import('./data-checks/pre-flight/strategy-preflight.component').then((m) => m.StrategyPreflightComponent),
          },
        ],
      },
    ],
  },
];
