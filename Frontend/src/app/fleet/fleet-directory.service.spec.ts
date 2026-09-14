import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { testLane } from './fleet-directory-testing';
import { FleetDirectoryService } from './fleet-directory.service';

describe('FleetDirectoryService', () => {
  afterEach(() => TestBed.inject(HttpTestingController).verify());

  function setup(): { directory: FleetDirectoryService; http: HttpTestingController } {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    return {
      directory: TestBed.inject(FleetDirectoryService),
      http: TestBed.inject(HttpTestingController),
    };
  }

  it('shares one cold load between callers and publishes the exact response atomically', async () => {
    const { directory, http } = setup();
    const first = directory.ensureLoaded();
    const second = directory.ensureLoaded();

    const request = http.expectOne('/api/broker-clerks');
    expect(directory.isLoading()).toBe(true);
    request.flush({ observed_at_ms: 10, clerks: [testLane()] });

    await expect(first).resolves.toEqual(await second);
    expect(directory.value()?.clerks).toHaveLength(1);
    expect(directory.error()).toBeUndefined();
  });

  it('retains a directory failure for the UI and rejects a cold guard load', async () => {
    const { directory, http } = setup();
    const pending = directory.ensureLoaded();

    http.expectOne('/api/broker-clerks').flush('offline', {
      status: 503,
      statusText: 'Unavailable',
    });

    await expect(pending).rejects.toMatchObject({ status: 503 });
    expect(directory.error()).toMatchObject({ status: 503 });
    expect(directory.value()).toBeUndefined();
    // Within the cooldown the stored error is replayed rather than re-requested,
    // so a sustained outage does not turn every navigation into another call.
    await expect(directory.ensureLoaded()).rejects.toMatchObject({ status: 503 });
  });

  it('recovers from a cold-load blip once the retry cooldown has passed', async () => {
    // The service is `providedIn: 'root'`. A stored error used to be permanent,
    // so one blip while the directory was cold disabled every lane-scoped
    // redirect and execution command for the whole browser session.
    const { directory, http } = setup();
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000);

    try {
      const cold = directory.ensureLoaded();
      http.expectOne('/api/broker-clerks').flush('offline', {
        status: 503,
        statusText: 'Unavailable',
      });
      await expect(cold).rejects.toMatchObject({ status: 503 });

      now.mockReturnValue(1_000 + 3_000);
      const retry = directory.ensureLoaded();
      http.expectOne('/api/broker-clerks').flush({ observed_at_ms: 20, clerks: [testLane()] });

      await expect(retry).resolves.toMatchObject({ observed_at_ms: 20 });
      expect(directory.value()?.clerks).toHaveLength(1);
    } finally {
      now.mockRestore();
    }
  });

  it('clears the stored error when a retry succeeds', async () => {
    const { directory, http } = setup();
    const now = vi.spyOn(Date, 'now').mockReturnValue(5_000);

    try {
      const cold = directory.ensureLoaded();
      http.expectOne('/api/broker-clerks').flush('offline', { status: 503, statusText: 'Unavailable' });
      await expect(cold).rejects.toMatchObject({ status: 503 });
      expect(directory.error()).toMatchObject({ status: 503 });

      now.mockReturnValue(5_000 + 3_000);
      const retry = directory.ensureLoaded();
      http.expectOne('/api/broker-clerks').flush({ observed_at_ms: 30, clerks: [testLane()] });
      await retry;

      expect(directory.error()).toBeUndefined();
    } finally {
      now.mockRestore();
    }
  });

  it('preserves a failed lane beside a healthy lane instead of collapsing the directory', async () => {
    const { directory, http } = setup();
    const pending = directory.ensureLoaded();
    const degraded = testLane({ clerk_id: 'clerk-degraded', lifecycle_state: 'unreachable' });

    http.expectOne('/api/broker-clerks').flush({
      observed_at_ms: 10,
      clerks: [testLane(), degraded],
    });

    await pending;
    expect(directory.lanesOf('alpaca')).toEqual([testLane(), degraded]);
  });
});
