import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { environment } from '../../environments/environment';
import { ExhaustiveRunService } from './exhaustive-run.service';

describe('ExhaustiveRunService', () => {
  it('loads the latest artifact for a walk-forward receipt', async () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const service = TestBed.inject(ExhaustiveRunService);
    const http = TestBed.inject(HttpTestingController);

    const pending = service.getLatestForWalkForward('wf-id');
    const request = http.expectOne(
      `${environment.pythonServiceUrl}/api/research/exhaustive-runs/by-walk-forward/wf-id`,
    );
    request.flush({ config: {}, result: {} });

    expect(await pending).toEqual({ config: {}, result: {} });
    http.verify();
  });

  it('maps a missing artifact to null', async () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const service = TestBed.inject(ExhaustiveRunService);
    const http = TestBed.inject(HttpTestingController);

    const pending = service.getLatestForWalkForward('missing');
    http.expectOne(
      `${environment.pythonServiceUrl}/api/research/exhaustive-runs/by-walk-forward/missing`,
    ).flush({}, { status: 404, statusText: 'Not Found' });

    expect(await pending).toBeNull();
    http.verify();
  });
});
