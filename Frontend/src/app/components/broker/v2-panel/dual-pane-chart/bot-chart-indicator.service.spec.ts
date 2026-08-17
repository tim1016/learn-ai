import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { BotChartIndicatorService } from './bot-chart-indicator.service';

describe('BotChartIndicatorService', () => {
  it('posts exact chart bars and indicator recipes to the Python authority', () => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const service = TestBed.inject(BotChartIndicatorService);
    const http = TestBed.inject(HttpTestingController);

    service.calculate('SPY', [{
      start_ms: 1_700_000_000_000,
      end_ms: 1_700_000_060_000,
      open: '99', high: '101', low: '98', close: '100', volume: 10, source: 'ibkr',
    }], [{ name: 'sma', params: { length: 2 } }]).subscribe();

    const request = http.expectOne((candidate) => candidate.url.endsWith('/api/chart/indicators'));
    expect(request.request.body).toEqual({
      symbol: 'SPY',
      bars: [{ t: 1_700_000_060_000, o: 99, h: 101, l: 98, c: 100, v: 10 }],
      indicators: [{ name: 'sma', params: { length: 2 } }],
    });
    request.flush({ symbol: 'SPY', indicators: [] });
    http.verify();
  });
});
