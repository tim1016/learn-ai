import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokerV2PanelService } from './broker-v2-panel.service';

describe('BrokerV2PanelService run evidence', () => {
  let service: BrokerV2PanelService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerV2PanelService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the current run from the account-independent run endpoint', async () => {
    const response = service.getCurrentRun('alpaca paper', 'sid/001');
    const request = http.expectOne(
      '/api/brokers/alpaca%20paper/bots/sid%2F001/runs/current',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ run_id: 'run-current' });

    await expect(response).resolves.toMatchObject({ run_id: 'run-current' });
  });

  it('loads exactly one previous run using the opaque server cursor', async () => {
    const response = service.getRunHistory('alpaca', 'sid-001', 'run/newest');
    const request = http.expectOne(
      (candidate) =>
        candidate.url === '/api/brokers/alpaca/bots/sid-001/runs/history' &&
        candidate.params.get('limit') === '1' &&
        candidate.params.get('cursor') === 'run/newest',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ runs: [], next_cursor: null });

    await expect(response).resolves.toEqual({ runs: [], next_cursor: null });
  });
});
