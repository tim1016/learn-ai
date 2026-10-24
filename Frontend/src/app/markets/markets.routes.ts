import { Routes } from '@angular/router';

import { OPTIONS_ROUTES } from './options/options.routes';

/**
 * The Markets place (ADR 0084): the list at `/markets`, and one instrument's
 * place at `/markets/:symbol/<tab>`.
 *
 * The instrument's shell owns the context its tabs share, so it stays mounted
 * while the tab or the instrument changes. A tab that is not routed yet has no
 * entry here and shows greyed in the strip (`UNAVAILABLE_INSTRUMENT_TABS`); a
 * tab leaves that list when its route is added below.
 */
export const MARKETS_ROUTES: Routes = [
  {
    path: '',
    pathMatch: 'full',
    loadComponent: () => import('./list/markets-list.component').then((m) => m.MarketsListComponent),
  },
  {
    path: ':symbol',
    loadComponent: () =>
      import('./workspace/instrument-workspace.component').then((m) => m.InstrumentWorkspaceComponent),
    children: [
      // A relative redirect keeps the query and the fragment.
      { path: '', pathMatch: 'full', redirectTo: 'chart' },
      {
        path: 'chart',
        pathMatch: 'full',
        loadComponent: () => import('./chart/instrument-chart.component').then((m) => m.InstrumentChartComponent),
      },
      {
        path: 'chart/news',
        pathMatch: 'full',
        loadComponent: () => import('./news/news-page.component').then((m) => m.NewsPageComponent),
      },
      ...OPTIONS_ROUTES,
    ],
  },
];
