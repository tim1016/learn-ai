/** #2639: gate writes are data-plane control calls, so they must reach the
 * dev proxy (the one layer that attaches the control secret) with the
 * browser's control intent. */
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import {
  DATA_PLANE_CONTROL_INTENT_HEADER,
  DATA_PLANE_CONTROL_INTENT_VALUE,
  dataPlaneControlIntentInterceptor,
} from '../../../../security/data-plane-control-intent.interceptor';
import { StrategyGatesService } from './strategy-gates.service';

function setUp(): { gates: StrategyGatesService; http: HttpTestingController } {
  TestBed.configureTestingModule({
    providers: [provideHttpClient(withInterceptors([dataPlaneControlIntentInterceptor])), provideHttpClientTesting()],
  });
  return { gates: TestBed.inject(StrategyGatesService), http: TestBed.inject(HttpTestingController) };
}

describe('StrategyGatesService (#2639)', () => {
  afterEach(() => TestBed.inject(HttpTestingController).verify());

  it('saves a gate through the proxy, on a relative path, with the control intent', () => {
    const { gates, http } = setUp();

    void gates.create('ema_crossover_signal', { label: 'g', expression: 'close - open', sign: 'gt', settings: {} });

    // An absolute data-plane URL would skip the proxy, and the data plane would refuse the write.
    const request = http.expectOne('/api/strategy-gates/ema_crossover_signal');
    expect(request.request.method).toBe('POST');
    expect(request.request.headers.get(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(DATA_PLANE_CONTROL_INTENT_VALUE);
    request.flush({});
  });
});
