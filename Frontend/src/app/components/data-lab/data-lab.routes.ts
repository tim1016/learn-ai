import { Routes } from '@angular/router';

import { redirectLegacyDataLab } from './data-lab-ingress';
import {
  createDataLabWorkspaceStore,
  DataLabWorkspaceStore,
} from './data-lab-workspace-store';

/* /data-lab route family.
 *
 * One shell component with lazy child routes (export / validate / returns).
 * The workspace store is provided on the parent route's injector so the shell
 * and every child share one instance — state survives child route navigation
 * and dies with the shell. The chart is no longer one of them: it moved to the
 * instrument place (`/markets/:symbol/chart`), and `/data-lab` and
 * `/data-lab/explore` redirect straight to the instrument their query names,
 * or to Markets when it names none. `/data-lab?mode=` still names the child
 * it always did, with its query and fragment intact; the shell then applies
 * that query to the workspace. */

export const DATA_LAB_ROUTES: Routes = [
  {
    path: '',
    providers: [
      // Component-scoped by construction: `createDataLabWorkspaceStore()`
      // builds a plain instance, never `providedIn: 'root'`.
      { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
    ],
    loadComponent: () =>
      import('./data-lab.component').then((m) => m.DataLabComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: redirectLegacyDataLab('instrument') },
      { path: 'explore', pathMatch: 'full', redirectTo: redirectLegacyDataLab('instrument') },
      {
        path: 'export',
        loadComponent: () =>
          import('./export/export.component').then((m) => m.ExportComponent),
      },
      {
        path: 'validate',
        loadComponent: () =>
          import('./validate/validate.component').then((m) => m.ValidateComponent),
      },
      {
        path: 'returns',
        loadComponent: () =>
          import('./returns-distribution/returns-distribution.component').then(
            (m) => m.ReturnsDistributionComponent,
          ),
      },
    ],
  },
];
