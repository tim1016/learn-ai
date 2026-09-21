import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { describe, it, expect, beforeEach } from 'vitest';
import { of, throwError, NEVER, type Observable } from 'rxjs';

import { VendorCatalogService, type VendorSymbolEntry } from './vendor-catalog.service';

function entry(overrides: Partial<VendorSymbolEntry> = {}): VendorSymbolEntry {
  return {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    asset_class: 'us_equity',
    exchange: 'NASDAQ',
    status: 'active',
   
    ...overrides,
  };
}

/** The one member of `HttpClient` the service touches. */
interface HttpGetStub {
  get(url: string): Observable<unknown>;
}

async function catalogFor(http: HttpGetStub): Promise<VendorCatalogService> {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [{ provide: HttpClient, useValue: http }],
  });
  const service = TestBed.inject(VendorCatalogService);
  // Reading the signal registers the resource; `tick` runs the effect that
  // starts the loader, and the stub resolves on the next macrotask.
  service.entries();
  TestBed.tick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  TestBed.tick();
  return service;
}

describe('VendorCatalogService', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('fetches the whole trimmed catalog once and exposes it', async () => {
    const rows = [entry(), entry({ symbol: 'OLD', status: 'inactive', })];
    let calls = 0;
    const service = await catalogFor({
      get: () => {
        calls++;
        return of(rows);
      },
    });

    expect(service.entries()).toEqual(rows);
    expect(service.unavailable()).toBeNull();
    expect(calls).toBe(1);
  });

  it('reports a dark catalog as a named outcome, never a partial list', async () => {
    const service = await catalogFor({
      get: () => throwError(() => new Error('catalog endpoint down')),
    });

    expect(service.entries()).toBeNull();
    expect(service.unavailable()).toContain('catalog endpoint down');
  });

  it('stays in flight — entries null, unavailable null — while the read runs', async () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: HttpClient, useValue: { get: () => NEVER } }],
    });
    const service = TestBed.inject(VendorCatalogService);
    service.entries();
    TestBed.tick();
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(service.entries()).toBeNull();
    expect(service.loading()).toBe(true);
    expect(service.unavailable()).toBeNull();
  });
});
