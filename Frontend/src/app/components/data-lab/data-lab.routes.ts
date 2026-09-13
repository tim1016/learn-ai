import { Routes } from '@angular/router';

import {
  createDataLabWorkspaceStore,
  DataLabWorkspaceStore,
} from './data-lab-workspace-store';

/* /data-lab route family (PRD 2026-09-12 §7.1).
 *
 * One shell component with three lazy child routes. The workspace store is
 * provided on the parent route's injector so the shell and every child
 * (explore / export / validate) share one instance — state survives child
 * route navigation and dies with the shell (FR-002). `/data-lab` redirects
 * to `/data-lab/explore` after the shell normalizes legacy query state
 * (PRD §14). */

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
      { path: '', pathMatch: 'full', redirectTo: 'explore' },
      {
        path: 'explore',
        loadComponent: () =>
          import('./explore/explore.component').then((m) => m.ExploreComponent),
      },
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
    ],
  },
];
