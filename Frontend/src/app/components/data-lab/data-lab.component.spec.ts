import { Component, signal } from '@angular/core';
import { Router, RouterOutlet, Routes, provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DataLabSessionService } from '../../services/data-lab-session.service';
import { RunSessionService } from '../../services/run-session.service';
import { DataLabComponent } from './data-lab.component';
import { createDataLabWorkspaceStore, DataLabWorkspaceStore } from './data-lab-workspace-store';

@Component({ selector: 'app-explore-stub', template: 'explore-stub' })
class ExploreStubComponent {}

@Component({ selector: 'app-export-stub', template: 'export-stub' })
class ExportStubComponent {}

@Component({ selector: 'app-validate-stub', template: 'validate-stub' })
class ValidateStubComponent {}

/** Host with a router outlet so the shell really routes in tests. */
@Component({ selector: 'app-shell-host', imports: [RouterOutlet], template: '<router-outlet />' })
class ShellHostComponent {}

const savedSession = {
  id: 'session-1',
  name: 'Apple review',
  createdAt: '2026-05-15T12:00:00Z',
  updatedAt: '2026-05-15T12:00:00Z',
  ticker: 'AAPL',
  fromDate: '2026-04-15',
  toDate: '2026-05-15',
  indicatorCount: 2,
  hasChart: false,
};

function idleRunSession() {
  return {
    state: signal<'idle' | 'fetching' | 'bundling'>('idle'),
    sessionId: signal<string | null>(null),
    dockState: signal<'idle' | 'active' | 'done' | 'error'>('idle'),
    headline: signal('idle — no run in flight'),
    headlineLevel: signal<'info' | 'success' | 'warn' | 'error'>('info'),
    progressPercent: signal<number | null>(null),
    etaText: signal<string | null>(null),
    canCancel: signal(false),
    log: signal([]),
    runMeta: signal(null),
    start: vi.fn(async () => undefined),
    cancel: vi.fn(async () => undefined),
  };
}

function shellRoutes(): Routes {
  return [
    {
      path: '',
      providers: [
        { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
      ],
      component: DataLabComponent,
      children: [
        { path: '', pathMatch: 'full', redirectTo: 'explore' },
        { path: 'explore', component: ExploreStubComponent },
        { path: 'export', component: ExportStubComponent },
        { path: 'validate', component: ValidateStubComponent },
      ],
    },
  ];
}

function baseProviders(runSession = idleRunSession()) {
  return [
    { provide: RunSessionService, useValue: runSession },
    {
      provide: DataLabSessionService,
      useValue: {
        listSessions: vi.fn(async () => [savedSession]),
        getSession: vi.fn(),
        saveSession: vi.fn(),
        updateSession: vi.fn(),
        renameSession: vi.fn(),
        deleteSession: vi.fn(),
      },
    },
  ];
}

/** Render the shell through a real router outlet (tabs, routing, dock). */
async function renderRoutedShell(runSession = idleRunSession()) {
  const result = await render(ShellHostComponent, {
    providers: [...baseProviders(runSession), provideRouter(shellRoutes())],
  });
  const router = result.fixture.componentRef.injector.get(Router);
  await waitFor(() => expect(router.url).toBe('/explore'));
  // The workspace store lives on the route injector — reach the routed
  // shell instance and read its store from there.
  const shellDebug = result.fixture.debugElement.query(
    (el) => el.componentInstance instanceof DataLabComponent,
  );
  if (!shellDebug) throw new Error('shell did not render');
  const shell = shellDebug.componentInstance as DataLabComponent;
  return { ...result, router, store: shell.store, shell };
}

/** Render the shell directly so its public ingress seam can be driven
 *  against a stubbed `router.url` (no real navigation happens — navigate
 *  is mocked). */
async function renderDirectShell(url: string) {
  const result = await render(DataLabComponent, {
    providers: [
      ...baseProviders(),
      // Rendering the shell directly bypasses the route injector, so the
      // route-level store provider from shellRoutes() never applies —
      // supply the same shell-owned store here.
      { provide: DataLabWorkspaceStore, useFactory: createDataLabWorkspaceStore },
      provideRouter(shellRoutes()),
    ],
  });
  const router = result.fixture.componentRef.injector.get(Router);
  // Let the router's initial navigation finish before stubbing router.url:
  // the shell re-runs the legacy ingress on NavigationEnd, and a late
  // initial event would re-populate the banner after dismissal.
  await waitFor(() => expect(router.url).toBe('/explore'));
  const navSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
  vi.spyOn(router, 'url', 'get').mockReturnValue(url);
  return { ...result, router, navSpy };
}

describe('DataLabComponent (shell)', () => {
  it('renders the three route tabs and redirects /data-lab to explore', async () => {
    const { router } = await renderRoutedShell();
    expect(router.url).toBe('/explore');
    expect(screen.getByRole('link', { name: 'Explore' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Build dataset' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Validate' })).toBeTruthy();
    expect(screen.getByText('explore-stub')).toBeTruthy();
  });

  it('navigates between child routes via the tabs', async () => {
    const { router } = await renderRoutedShell();
    await userEvent.click(screen.getByRole('link', { name: 'Validate' }));
    await waitFor(() => expect(router.url).toBe('/validate'));
    expect(screen.getByText('validate-stub')).toBeTruthy();

    await userEvent.click(screen.getByRole('link', { name: 'Build dataset' }));
    await waitFor(() => expect(router.url).toBe('/export'));
    expect(screen.getByText('export-stub')).toBeTruthy();
  });

  it('resolves a legacy mode=build URL to export with validated params and no run', async () => {
    const { navSpy, fixture } = await renderDirectShell(
      '/data-lab?mode=build&ticker=AAPL&from=2026-04-15&to=2026-05-15',
    );
    fixture.componentInstance.runLegacyIngress();

    expect(navSpy).toHaveBeenCalledWith(
      ['/data-lab/export'],
      {
        queryParams: { ticker: 'AAPL', from: '2026-04-15', to: '2026-05-15' },
        replaceUrl: true,
      },
    );
    // URL state populates the committed workspace (PRD §14)…
    expect(fixture.componentInstance.store.committedTicker()).toBe('AAPL');
    expect(fixture.componentInstance.store.committedWindow()?.startMsUtc).toBe(
      Date.UTC(2026, 3, 15),
    );
  });

  it('applies a redirected /data-quality bookmark on the validate child URL', async () => {
    const { navSpy, fixture } = await renderDirectShell(
      '/data-lab/validate?ticker=AAPL&from=2026-04-15&to=2026-05-15',
    );
    fixture.componentInstance.runLegacyIngress();

    expect(navSpy).toHaveBeenCalledWith(
      ['/data-lab/validate'],
      {
        queryParams: { ticker: 'AAPL', from: '2026-04-15', to: '2026-05-15' },
        replaceUrl: true,
      },
    );
    expect(fixture.componentInstance.store.committedTicker()).toBe('AAPL');
    expect(fixture.componentInstance.store.committedWindow()?.startMsUtc).toBe(
      Date.UTC(2026, 3, 15),
    );
    expect(fixture.componentInstance.store.committedWindow()?.endMsUtc).toBe(
      Date.UTC(2026, 4, 15),
    );
  });

  it('surfaces ingress warnings in a dismissible banner', async () => {
    const { fixture } = await renderDirectShell('/data-lab?mode=bogus&frobnicate=1');
    fixture.componentInstance.runLegacyIngress();
    fixture.detectChanges();

    expect(screen.getByText(/Dropped invalid mode "bogus"/)).toBeTruthy();
    expect(screen.getByText(/Dropped unknown query key "frobnicate"/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    // Zoneless: raw user-event dispatch does not schedule change detection,
    // so flush manually before asserting the banner is gone.
    fixture.detectChanges();
    expect(screen.queryByText(/Dropped invalid mode/)).toBeNull();
  });

  it('seeds the thirteen-indicator default recipe into a fresh workspace', async () => {
    const { store } = await renderRoutedShell();
    expect(store.indicators().length).toBe(13);
  });

  it('hosts the shell-owned run dock and opens the saved-setups drawer', async () => {
    await renderRoutedShell();
    expect(document.querySelector('app-run-dock')).not.toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'Saved setups' }));
    expect(await screen.findByText('Apple review')).toBeTruthy();
  });
});
