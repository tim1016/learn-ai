import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../environments/environment';
import { IndicatorCatalogService } from './indicator-catalog.service';

const CATALOG_URL = `${environment.pythonServiceUrl}/api/dataset/available`;

function setup(): { service: IndicatorCatalogService; http: HttpTestingController } {
  TestBed.configureTestingModule({
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  return {
    service: TestBed.inject(IndicatorCatalogService),
    http: TestBed.inject(HttpTestingController),
  };
}

describe('IndicatorCatalogService failed', () => {
  it('is false before any load and after a successful load', async () => {
    const { service, http } = setup();
    expect(service.failed()).toBe(false);

    const load = service.load();
    http.expectOne(CATALOG_URL).flush({ success: true, categories: {}, total: 0 });
    await load;

    expect(service.failed()).toBe(false);
  });

  it('is true after the request fails', async () => {
    const { service, http } = setup();

    const load = service.load();
    http.expectOne(CATALOG_URL).flush('boom', { status: 500, statusText: 'Internal Server Error' });
    await load;

    expect(service.failed()).toBe(true);
  });

  it('is true when the backend reports success: false', async () => {
    const { service, http } = setup();

    const load = service.load();
    http.expectOne(CATALOG_URL).flush({ success: false, categories: {}, total: 0 });
    await load;

    expect(service.failed()).toBe(true);
  });
});
