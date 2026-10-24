import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Router, provideRouter, withComponentInputBinding, withRouterConfig } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { waitFor } from '@testing-library/angular';
import { signal, type Provider } from '@angular/core';
import type { Routes } from '@angular/router';

import { environment } from '../../../environments/environment';
import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { DataLabRangePresetsService, type ChartRangePreset } from '../../services/data-lab-range-presets.service';
import { RunSessionService } from '../../services/run-session.service';
import type { VendorSymbolEntry } from '../../shared/symbol-catalog/vendor-catalog.service';
import { ScopedHealthService } from '../../shared/scoped-health/scoped-health.service';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { facts } from '../facts/testing/facts-fixtures';
import { InstrumentFactsService, type FactsRead } from '../facts/instrument-facts.service';
import { InstrumentContextService } from '../instrument-context.service';
import { MARKETS_ROUTES } from '../markets.routes';
import { InstrumentWorkspaceComponent } from '../workspace/instrument-workspace.component';

/*
 * An instrument's place, mounted through the real routes with the lake and the
 * data plane replaced by the HTTP testing backend, for the specs of its tabs.
 * Pure helpers only: it imports no test runner, so any spec can use it.
 */

export const FROM = '2026-04-15';
export const TO = '2026-05-15';
export const WINDOW_QUERY = `from=${FROM}&to=${TO}`;

export const INSTRUMENT_DATA_URL = `${environment.pythonServiceUrl}/api/data-lake/instrument-data`;
export const CHART_URL = `${environment.pythonServiceUrl}/api/chart/data`;

/** Python's 1Y range, as the place reads it for an address with no window. */
export const PRESET_1Y: ChartRangePreset = {
  key: '1Y',
  label: 'Past year',
  start_ms_utc: Date.UTC(2025, 8, 22),
  end_ms_utc: Date.UTC(2026, 8, 18) + 86_400_000 - 1,
  session_count: 251,
  estimated_bars_per_timeframe: { '1D': 251 },
};

const vendor = (symbol: string): VendorSymbolEntry => ({
  symbol,
  name: `${symbol} Inc.`,
  exchange: 'XNAS',
  asset_class: 'us_equity',
  status: 'active',
});

/** The lake holds SPY and QQQ, and the vendor lists both. */
export function placeWorld() {
  return fakePickerWorld(
    [
      { symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
      { symbol: 'QQQ', name: 'Invesco QQQ', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
    ],
    [vendor('SPY'), vendor('QQQ')],
  );
}

/**
 * The dataset run the place's dock shows, with no run in flight. The dock reads the
 * app-wide run session, which would start reading the job registry on its own; a spec
 * that is about a run provides its own in place of this one.
 */
export function idleRunSession() {
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
    clearLog: () => undefined,
    start: async (_payload: Record<string, unknown>) => undefined,
    cancel: async () => undefined,
  };
}

/** The lake notice's reader, answering that nothing needs a note: a spec about another tab asks the data plane nothing. */
export const QUIET_HEALTH = { read: async () => ({ generated_ms: 0, items: [] }) };

/** The header's facts reader, answering each symbol with a quote: a spec about a tab asks the data plane nothing about the header. */
export const QUIET_FACTS = {
  read: async (symbols: readonly string[]): Promise<FactsRead> => ({ kind: 'ok', facts: new Map(symbols.map((symbol) => [symbol, facts(symbol)])) }),
};

/** Routes beside the place's, for a spec that reaches the place from an old address. */
export async function openPlace(url: string, extraProviders: readonly Provider[] = [], extraRoutes: Routes = []) {
  const world = placeWorld();
  TestBed.configureTestingModule({
    providers: [
      provideRouter(
        [{ path: 'markets', loadChildren: () => MARKETS_ROUTES }, ...extraRoutes],
        withComponentInputBinding(),
        withRouterConfig({ paramsInheritanceStrategy: 'always' }),
      ),
      provideHttpClient(),
      provideHttpClientTesting(),
      ...world.providers,
      { provide: DataLabRangePresetsService, useValue: { presets: async () => [PRESET_1Y] } },
      { provide: RunSessionService, useValue: idleRunSession() },
      { provide: ScopedHealthService, useValue: QUIET_HEALTH },
      { provide: InstrumentFactsService, useValue: QUIET_FACTS },
      ...extraProviders,
    ],
  });
  const harness = await RouterTestingHarness.create();
  const http = TestBed.inject(HttpTestingController);
  const router = TestBed.inject(Router);
  await harness.navigateByUrl(url);
  harness.fixture.detectChanges();
  const workspace = () => harness.fixture.debugElement.query(By.directive(InstrumentWorkspaceComponent));
  return {
    harness,
    http,
    router,
    context: () => workspace().injector.get(InstrumentContextService),
    store: () => workspace().injector.get(DataLabWorkspaceStore),
  };
}

export type OpenedPlace = Awaited<ReturnType<typeof openPlace>>;

/** The catalog read every chart tab starts; its answer is empty and beside the point. */
export function flushCatalog(http: HttpTestingController): void {
  http.match(`${environment.pythonServiceUrl}/api/dataset/available`).forEach((request) =>
    request.flush({ success: true, categories: {}, total: 0 }),
  );
}

/** What the chart endpoint answers: one bar, closing at `close`, with the quality evidence of a clean load. */
export function chartResponse(close: number) {
  return {
    bars: [{ t: 1_764_167_400_000, o: close, h: close, l: close, c: close, v: 1000 }],
    indicators: [],
    quality: {
      raw_bar_count: 1,
      resampled_bar_count: 1,
      duplicates_removed: 0,
      gaps_found: 0,
      largest_gap_minutes: 0,
      missing_sessions: 0,
      session_coverage_pct: 100,
      synthetic_bars: 0,
      gap_details: [],
      missing_session_dates: [],
    },
    bar_sources: null,
    allowed_timeframes: ['1m', '5m', '1D'],
    estimated_bars_per_timeframe: {},
    recommended_timeframe: '1D',
    meta: { cached_resample: false, cached_indicators: false },
  };
}

/** The one chart request the tab sends once the deferred request has run. Fails if there are none, or several. */
export async function chartRequest(http: HttpTestingController): Promise<TestRequest> {
  return waitFor(() => {
    flushCatalog(http);
    return http.expectOne(CHART_URL);
  });
}
