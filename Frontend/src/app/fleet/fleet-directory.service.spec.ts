import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

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
    await expect(directory.ensureLoaded()).rejects.toMatchObject({ status: 503 });
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
