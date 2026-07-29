import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { OfflineReplayService } from './offline-replay.service';

describe('OfflineReplayService', () => {
  it('launches the fixed two-symbol replay contract', async () => {
    TestBed.configureTestingModule({
      providers: [
        OfflineReplayService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    const service = TestBed.inject(OfflineReplayService);
    const http = TestBed.inject(HttpTestingController);
    const pending = service.createSession({
      session_date_ms: 1_775_000_000_000,
      symbols: ['SPY', 'TSLA'],
      playback_minutes: 60,
      speed: 60,
      initial_cash_usd: '100000',
      auto_fetch: true,
    });

    const request = http.expectOne('/api/offline-replay/sessions');
    expect(request.request.method).toBe('POST');
    expect(request.request.body.symbols).toEqual(['SPY', 'TSLA']);
    request.flush({ session_id: 'replay-1' });

    await expect(pending).resolves.toMatchObject({ session_id: 'replay-1' });
    http.verify();
  });

  it('sends step as a session command', async () => {
    TestBed.configureTestingModule({
      providers: [
        OfflineReplayService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    const service = TestBed.inject(OfflineReplayService);
    const http = TestBed.inject(HttpTestingController);
    const pending = service.command('a/b', { action: 'step' });

    const request = http.expectOne(
      '/api/offline-replay/sessions/a%2Fb/commands',
    );
    expect(request.request.body).toEqual({ action: 'step' });
    request.flush({ session_id: 'a/b', status: 'paused' });

    await expect(pending).resolves.toMatchObject({ status: 'paused' });
    http.verify();
  });
});
