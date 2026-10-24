import type { CanMatchFn, Routes } from '@angular/router';

/** A setup's address always carries its tab, so a bare second segment names no setup and falls through to the not-found page. */
const carriesATab: CanMatchFn = (_route, segments) => segments.length >= 2;

/**
 * The Options setups place (ADR 0084). A saved name is one segment and a tab
 * is the next, so a bare second segment never names a setup; evidence has
 * three segments after the group and needs no name.
 */
export const OPTIONS_SETUPS_ROUTES: Routes = [
  {
    path: '',
    pathMatch: 'full',
    loadComponent: () => import('./index/options-setups-index.component').then((m) => m.OptionsSetupsIndexComponent),
  },
  {
    path: 'evidence/:kind/:subjectId',
    loadComponent: () => import('../components/options-evidence/options-evidence-page.component').then((m) => m.OptionsEvidencePageComponent),
  },
  {
    path: ':name',
    canMatch: [carriesATab],
    loadComponent: () => import('./workspace/options-setup-workspace.component').then((m) => m.OptionsSetupWorkspaceComponent),
    children: [
      {
        path: 'overview',
        loadComponent: () => import('./workspace/overview/options-setup-overview.component').then((m) => m.OptionsSetupOverviewComponent),
      },
      {
        path: 'define',
        // The setup's read-back rail stays in view beside the form while the page scrolls.
        data: { stickyContent: true },
        loadComponent: () => import('./workspace/define/options-setup-define.component').then((m) => m.OptionsSetupDefineComponent),
      },
    ],
  },
];
