import { Component, signal } from '@angular/core';
import { Router, RouterOutlet, Routes, provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { DataLabSessionService } from '../../services/data-lab-session.service';
import {
  ChartRangePreset,
  DataLabRangePresetsService,
} from '../../services/data-lab-range-presets.service';
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
    { provide: DataLabRangePresetsService, useValue: { presets: vi.fn(async () => RANGE_PRESET_STUBS) } },
  ];
}

/** Calendar-resolved presets as Python would resolve them for a Saturday
 *  (2026-09-12): both windows end Friday 2026-09-11, the 1D window is that
 *  single session, 6M is 126 sessions back. */
const preset1D: ChartRangePreset = {
  key: '1D',
  label: 'Past day',
  start_date: '2026-09-11',
  end_date: '2026-09-11',
  start_ms_utc: Date.UTC(2026, 8, 11),
  end_ms_utc: Date.UTC(2026, 8, 11),
  session_count: 1,
  estimated_bars_per_timeframe: { '1m': 390, '5m': 78, '1D': 1 },
};

const preset6M: ChartRangePreset = {
  key: '6M',
  label: 'Past 6 months',
  start_date: '2026-03-13',
  end_date: '2026-09-11',
  start_ms_utc: Date.UTC(2026, 2, 13),
  end_ms_utc: Date.UTC(2026, 8, 11),
  session_count: 126,
  estimated_bars_per_timeframe: { '1m': 49_140, '5m': 9_828, '1D': 126 },
};

const RANGE_PRESET_STUBS: readonly ChartRangePreset[] = [preset1D, preset6M];

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

  // ── Calendar-resolved quick ranges ──────────────────────────
  it('renders the quick-range chips Python resolved', async () => {
    await renderRoutedShell();
    const group = screen.getByRole('group', { name: 'Quick ranges' });
    expect(group.querySelector('button')?.textContent?.trim()).toBe('1D');
    expect(screen.getByRole('button', { name: '6M' })).toBeTruthy();
  });

  it('applies a preset verbatim: server window committed, chart refresh requested', async () => {
    const { store, fixture } = await renderRoutedShell();
    store.patchDraft({ ticker: 'SPY' });
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: '6M' }));
    fixture.detectChanges();

    expect(store.committedWindow()).toEqual({
      startMsUtc: preset6M.start_ms_utc,
      endMsUtc: preset6M.end_ms_utc,
    });
    expect(store.chartRefreshRequests()).toBe(1);
    // The matching chip is marked active; the other is not.
    const active = document.querySelector('.dl-shell__preset--active');
    expect(active?.textContent?.trim()).toBe('6M');
  });

  it('gives the 1D preset an intraday default timeframe so it is not one candle', async () => {
    const { store, fixture } = await renderRoutedShell();
    store.patchDraft({ ticker: 'SPY' });
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: '1D' }));
    fixture.detectChanges();

    const draft = store.draft();
    expect(draft.timespan).toBe('minute');
    expect(draft.multiplier).toBe(5);
    // A single-day window (start == end) must commit — equality names one
    // trading date under the interim date-anchor semantics, not an empty
    // range — and trigger the chart refresh.
    expect(store.committedWindow()).toEqual({
      startMsUtc: preset1D.start_ms_utc,
      endMsUtc: preset1D.end_ms_utc,
    });
    expect(store.chartRefreshRequests()).toBe(1);
  });

  it('leaves a blank-ticker preset click in the draft with the commit error, no refresh', async () => {
    const { store, fixture } = await renderRoutedShell();

    await userEvent.click(screen.getByRole('button', { name: '6M' }));
    fixture.detectChanges();

    expect(store.committedWindow()).toBeNull();
    expect(store.draft().window).toEqual({
      startMsUtc: preset6M.start_ms_utc,
      endMsUtc: preset6M.end_ms_utc,
    });
    expect(store.chartRefreshRequests()).toBe(0);
    expect(screen.getByText('Ticker is required')).toBeTruthy();
  });

  it('requests a chart refresh when Apply scope commits successfully', async () => {
    const { store, fixture } = await renderRoutedShell();
    store.patchDraft({
      ticker: 'SPY',
      window: { startMsUtc: preset6M.start_ms_utc, endMsUtc: preset6M.end_ms_utc },
    });
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Apply scope' }));
    fixture.detectChanges();

    expect(store.chartRefreshRequests()).toBe(1);
    expect(screen.queryByText('Ticker is required')).toBeNull();
  });
});
