import { Routes } from '@angular/router';

import { dataCheckRoute } from '../../operations/data-checks/data-checks';
import { redirectKeepingState } from '../../shell/route-redirects';
import { redirectLegacyDataLab } from './data-lab-ingress';

/* /data-lab: addresses people saved, and nothing else.
 *
 * Its chart, Returns study, dataset builder and saved setups moved under the
 * instrument they are for (`/markets/:symbol/(chart|returns|data|data/build)`),
 * and Validate and the option-archive card moved to Operations, so every
 * address here is a redirect straight to the page it became, with its query and
 * fragment — and, with no instrument named, to Markets. */

export const DATA_LAB_ROUTES: Routes = [
  { path: '', pathMatch: 'full', redirectTo: redirectLegacyDataLab('chart') },
  { path: 'explore', pathMatch: 'full', redirectTo: redirectLegacyDataLab('chart') },
  { path: 'export', pathMatch: 'full', redirectTo: redirectLegacyDataLab('build') },
  { path: 'returns', pathMatch: 'full', redirectTo: redirectLegacyDataLab('returns') },
  // Validate became Operations → Data checks → TradingView comparison; its query goes with it, and the comparison reads the `ticker` from it.
  { path: 'validate', pathMatch: 'full', redirectTo: redirectKeepingState(dataCheckRoute('tradingview')) },
];
