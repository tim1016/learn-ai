import { Routes } from '@angular/router';

import { DEFAULT_OPTIONS_VIEW } from './options-views';

/**
 * An instrument's Options tab (`/markets/:symbol/options`): the views it
 * routes, under the tab's strip. Mounted among the instrument's tabs by
 * `markets.routes.ts`. A view that is not routed yet has no entry here and
 * shows greyed in the strip (`UNAVAILABLE_OPTIONS_VIEWS`); it leaves that
 * list when its route is added below.
 */
export const OPTIONS_ROUTES: Routes = [
  {
    path: 'options',
    loadComponent: () => import('./options-tab.component').then((m) => m.OptionsTabComponent),
    children: [
      // A relative redirect keeps the query and the fragment.
      { path: '', pathMatch: 'full', redirectTo: DEFAULT_OPTIONS_VIEW },
      {
        path: 'chain',
        pathMatch: 'full',
        loadComponent: () => import('./chain/options-chain.component').then((m) => m.OptionsChainComponent),
      },
    ],
  },
];
