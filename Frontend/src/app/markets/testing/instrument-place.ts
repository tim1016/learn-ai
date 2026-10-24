import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Router, provideRouter, withComponentInputBinding, withRouterConfig } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import type { Provider } from '@angular/core';

import { environment } from '../../../environments/environment';
import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { DataLabRangePresetsService, type ChartRangePreset } from '../../services/data-lab-range-presets.service';
import type { InstrumentDataReport } from '../../shared/data-lake';
import type { VendorSymbolEntry } from '../../shared/symbol-catalog/vendor-catalog.service';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
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
export const RETURNS_URL = `${environment.pythonServiceUrl}/api/research/return-distribution`;

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

export async function openPlace(url: string, extraProviders: readonly Provider[] = []) {
  const world = placeWorld();
  TestBed.configureTestingModule({
    providers: [
      provideRouter(
        [{ path: 'markets', loadChildren: () => MARKETS_ROUTES }],
        withComponentInputBinding(),
        withRouterConfig({ paramsInheritanceStrategy: 'always' }),
      ),
      provideHttpClient(),
      provideHttpClientTesting(),
      ...world.providers,
      { provide: DataLabRangePresetsService, useValue: { presets: async () => [PRESET_1Y] } },
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

type Session = InstrumentDataReport['sessions'][number];

const DAY_MS = 86_400_000;

/** Session `n` of a window that opens on 2026-04-15's session. */
export function dataSession(n: number, state: Session['state'], extra: Partial<Session> = {}): Session {
  return { trading_date_ms: Date.UTC(2026, 3, 15, 13, 30) + n * DAY_MS, state, ...extra };
}

/**
 * A reading as Python sends it. The summary is counted from the sessions the
 * caller names, the way Python counts it, so a spec states sessions and reads
 * numbers off them.
 */
export function dataReport(
  sessions: readonly Session[],
  overrides: Partial<InstrumentDataReport> = {},
): InstrumentDataReport {
  const count = (state: Session['state']) => sessions.filter((s) => s.state === state).length;
  const unfinished = count('forming') + count('upcoming');
  return {
    market: 'usa',
    symbol: 'SPY',
    data_type: 'trade',
    resolution: 'minute',
    price_adjustment_mode: 'polygon_split_adjusted',
    data_root_id: '00000000-0000-0000-0000-000000000000',
    provider_history_start_ms: Date.UTC(2021, 9, 26, 12),
    as_of_ms: Date.UTC(2026, 9, 24, 12),
    summary: {
      calendar_day_count: sessions.length,
      session_count: sessions.length,
      ready: count('ready'),
      building: count('building'),
      needs_rebuild: count('needs_rebuild'),
      missing: count('missing'),
      failed: count('failed'),
      forming: count('forming'),
      upcoming: count('upcoming'),
      fetchable: sessions.filter((s) => s.fetchable).length,
      before_provider_history: sessions.filter((s) => s.blocked_by === 'before_provider_history').length,
      provider_no_data: sessions.filter((s) => s.blocked_by === 'provider_no_data').length,
      run_readable: sessions.length > unfinished && count('ready') === sessions.length - unfinished,
    },
    sessions: [...sessions],
    fill_window: null,
    daily_rollup: { state: 'current' },
    ...overrides,
  };
}
