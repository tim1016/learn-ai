import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { ScopedHealthService } from './scoped-health.service';
import { projection } from './testing/scoped-health-fixtures';

function setup() {
  TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
  return { service: TestBed.inject(ScopedHealthService), http: TestBed.inject(HttpTestingController) };
}

describe('the scoped-health read', () => {
  it('sends each operation and the instrument, in the data plane\'s upper-case names', async () => {
    const { service, http } = setup();

    const read = service.read({ operations: ['options_history', 'recovery'], instrument: ' spy ' });
    const request = http.expectOne((candidate) => candidate.url === '/api/scoped-health');
    request.flush(projection());
    await read;

    expect(request.request.method).toBe('GET');
    expect(request.request.params.getAll('operation')).toEqual(['options_history', 'recovery']);
    expect(request.request.params.get('instrument')).toBe('SPY');
  });

  it('asks for everything when it is asked for nothing', async () => {
    const { service, http } = setup();

    const read = service.read();
    const request = http.expectOne('/api/scoped-health');
    request.flush(projection());
    await read;

    expect(request.request.params.keys()).toEqual([]);
  });
});
