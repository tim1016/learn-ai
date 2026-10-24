import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { fleetPage } from '../../../signal-strategies/testing/fixtures';
import { BotHistoryService, RUNNING_BOTS_PAGE_SIZE } from './bot-history.service';

describe('BotHistoryService.readRunning', () => {
  let service: BotHistoryService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(BotHistoryService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('asks the coordinator for one strategy’s running bots, at its largest page, so it filters before it pages', async () => {
    const pending = service.readRunning('ema_crossover_signal', 2);

    const request = http.expectOne((r) => r.url.endsWith('/api/broker-clerks/aggregate/bot-history'));
    expect(request.request.params.get('status')).toBe('running');
    expect(request.request.params.get('strategy_key')).toBe('ema_crossover_signal');
    expect(request.request.params.get('page')).toBe('2');
    expect(request.request.params.get('page_size')).toBe(String(RUNNING_BOTS_PAGE_SIZE));
    request.flush(fleetPage());
    await pending;
  });

  it('leaves the strategy filter off when every strategy’s running bots are wanted', async () => {
    const pending = service.readRunning(null, 1);

    const request = http.expectOne((r) => r.url.endsWith('/api/broker-clerks/aggregate/bot-history'));
    expect(request.request.params.has('strategy_key')).toBe(false);
    request.flush(fleetPage());
    await pending;
  });
});
