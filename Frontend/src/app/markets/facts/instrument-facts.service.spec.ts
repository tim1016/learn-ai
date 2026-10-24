import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { environment } from '../../../environments/environment';
import { facts } from './testing/facts-fixtures';
import { InstrumentFactsService, MAX_FACT_SYMBOLS } from './instrument-facts.service';

const URL = `${environment.pythonServiceUrl}/api/markets/instrument-facts`;

function setup() {
  TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
  return { service: TestBed.inject(InstrumentFactsService), http: TestBed.inject(HttpTestingController) };
}

describe('InstrumentFactsService', () => {
  it('asks once for every symbol it is given and answers each by its symbol', async () => {
    const { service, http } = setup();

    const read = service.read(['SPY', 'QQQ']);
    const request = http.expectOne((r) => r.url === URL);
    expect(request.request.method).toBe('GET');
    expect(request.request.params.get('symbols')).toBe('SPY,QQQ');
    request.flush({ generated_ms: 1, closes_adjustment: 'polygon_split_adjusted', facts: [facts('QQQ'), facts('SPY')] });

    const answer = await read;
    expect(answer.kind).toBe('ok');
    expect(answer.kind === 'ok' && [...answer.facts.keys()]).toEqual(['QQQ', 'SPY']);
    http.verify();
  });

  it('resolves a refused or failed read as failed with the server\'s sentence, and does not reject', async () => {
    const { service, http } = setup();

    const read = service.read(['SPY']);
    http.expectOne((r) => r.url === URL).flush({ detail: 'The trading calendar could not name the last completed session.' }, { status: 503, statusText: 'Service Unavailable' });

    const answer = await read;
    expect(answer).toEqual({ kind: 'failed', message: 'The trading calendar could not name the last completed session.' });
  });

  it.each([[0], [MAX_FACT_SYMBOLS + 1]])('refuses to send a batch of %i symbols, since the server\'s bound is a caller\'s mistake', async (count) => {
    const { service, http } = setup();

    await expect(service.read(Array.from({ length: count }, (_, index) => `A${index}`))).rejects.toThrow(RangeError);
    http.expectNone(() => true);
  });
});
