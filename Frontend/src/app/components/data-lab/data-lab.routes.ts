import { Routes } from '@angular/router';

import { redirectLegacyDataLab } from './data-lab-ingress';

/* /data-lab: what is left of the Data Lab.
 *
 * Its chart, Returns study, dataset builder and saved setups moved under the
 * instrument they are for (`/markets/:symbol/(chart|returns|data|data/build)`),
 * so every address here but one is a redirect straight to the page it became,
 * with its query and fragment — and with no instrument named, to Markets. The
 * one page still here is Validate, with the option-archive card above it:
 * Operations (#3498, #3499) takes both, and then this file goes. */

export const DATA_LAB_ROUTES: Routes = [
  {
    path: '',
    loadComponent: () => import('./data-lab.component').then((m) => m.DataLabComponent),
    children: [
      { path: '', pathMatch: 'full', redirectTo: redirectLegacyDataLab('chart') },
      { path: 'explore', pathMatch: 'full', redirectTo: redirectLegacyDataLab('chart') },
      { path: 'export', pathMatch: 'full', redirectTo: redirectLegacyDataLab('build') },
      { path: 'returns', pathMatch: 'full', redirectTo: redirectLegacyDataLab('returns') },
      {
        path: 'validate',
        loadComponent: () => import('./validate/validate.component').then((m) => m.ValidateComponent),
      },
    ],
  },
];
