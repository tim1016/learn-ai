import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { LiveRunsService } from './live-runs.service';
import type { HostRunnerActionResponse } from '../api/live-runs.types';

/**
 * Start/Stop must go through the data plane (`/api/live-instances/runs/...`),
 * NOT directly to the daemon. The daemon enforces a mandatory
 * `X-Live-Runner-Token` on every actuation route (ADR 0007), and the browser
 * must never hold that shared secret — so the data plane forwards it
 * server-side. A regression here (calling `environment.liveRunnerDaemonUrl`
 * directly) reintroduces the 401 that made the UI Start/Stop buttons unusable.
 */
describe('LiveRunsService start/stop proxy', () => {
  let service: LiveRunsService;
  let httpMock: HttpTestingController;

  const accepted: HostRunnerActionResponse = {
    accepted: true,
    process: {
      state: 'running',
      run_id: 'run-abc',
      pid: 1,
      started_at_ms: 1,
      ended_at_ms: null,
      exit_code: null,
      command: [],
      log_path: '/x',
      message: 'active',
    },
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        LiveRunsService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    service = TestBed.inject(LiveRunsService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('startHostRunner posts to the data-plane proxy, not the daemon', async () => {
    const promise = service.startHostRunner('run-abc', {
      readonly: false,
      hydrate_policy: 'optional',
      strategy: 'spy_ema_crossover',
      max_orders_per_day: 4,
      ibkr_host: '127.0.0.1',
    });

    const req = httpMock.expectOne('/api/live-instances/runs/run-abc/start');
    expect(req.request.method).toBe('POST');
    expect(req.request.url.startsWith('http')).toBe(false); // relative — proxied, not daemon-direct
    expect(req.request.body.readonly).toBe(false);
    req.flush(accepted);

    await expect(promise).resolves.toEqual(accepted);
  });

  it('stopHostRunner posts to the data-plane proxy', async () => {
    const promise = service.stopHostRunner('run-abc', { force: false });

    const req = httpMock.expectOne('/api/live-instances/runs/run-abc/stop');
    expect(req.request.method).toBe('POST');
    expect(req.request.url.startsWith('http')).toBe(false);
    req.flush(accepted);

    await expect(promise).resolves.toEqual(accepted);
  });

  it('encodes the run id in the proxy path', async () => {
    const promise = service.stopHostRunner('run/with space', { force: true });

    const req = httpMock.expectOne('/api/live-instances/runs/run%2Fwith%20space/stop');
    expect(req.request.method).toBe('POST');
    req.flush(accepted);

    await promise;
  });
});
