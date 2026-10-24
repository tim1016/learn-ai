import { Routes } from '@angular/router';

import { DEFAULT_OPTIONS_VIEW } from './options-views';

/**
 * An instrument's Options tab (`/markets/:symbol/options`): the views it
 * routes, under the tab's strip. Mounted among the instrument's tabs by
 * `markets.routes.ts`.
 */
export const OPTIONS_ROUTES: Routes = [
  {
    path: 'options',
    loadComponent: () => import('./options-tab.component').then((m) => m.OptionsTabComponent),
    children: [
      // A relative redirect keeps the query and the fragment.
      { path: '', pathMatch: 'full', redirectTo: DEFAULT_OPTIONS_VIEW },
      {
        path: 'surface',
        pathMatch: 'full',
        loadComponent: () => import('./surface/options-surface.component').then((m) => m.OptionsSurfaceComponent),
      },
      {
        path: 'pricing',
        pathMatch: 'full',
        loadComponent: () => import('./pricing/options-pricing.component').then((m) => m.OptionsPricingComponent),
      },
    ],
  },
];
