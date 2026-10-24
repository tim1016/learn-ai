import type { Routes } from '@angular/router';

import { redirectKeepingState } from '../shell/route-redirects';
import { operationsTabRoute } from './operations-tabs';

/**
 * The `/operations` route family (ADR 0084): the Operations header over one
 * child per tab. The tab ids are the URL segments declared in
 * `operations-tabs.ts`.
 *
 * Math checks and Data checks are placeholders until their content moves here
 * (#3499): that slice replaces each `OperationsPendingTabComponent` route with
 * its page and deletes the placeholder.
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
        data: { tab: 'math-checks' },
        loadComponent: () =>
          import('./pending/operations-pending-tab.component').then((m) => m.OperationsPendingTabComponent),
      },
      {
        path: 'data-checks',
        data: { tab: 'data-checks' },
        loadComponent: () =>
          import('./pending/operations-pending-tab.component').then((m) => m.OperationsPendingTabComponent),
      },
    ],
  },
];
