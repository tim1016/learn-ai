import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ApplicationRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../date/et-midnight';
import { TradingCalendarService } from './trading-calendar.service';

const FRIDAY = { session_open_ms: [Date.UTC(2026, 9, 2, 13, 30)], session_close_ms: [Date.UTC(2026, 9, 2, 20)], holidays: [] };

describe('TradingCalendarService', () => {
  let service: TradingCalendarService;
  let http: HttpTestingController;

  const isCalendarRead = (request: { url: string }): boolean => request.url.endsWith('/api/calendar/sessions');
  const settle = async (): Promise<void> => {
    await TestBed.inject(ApplicationRef).whenStable();
  };

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(TradingCalendarService);
    http = TestBed.inject(HttpTestingController);
    // Saturday 2026-10-03, noon ET.
    vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout'] });
    vi.setSystemTime(Date.UTC(2026, 9, 3, 16));
  });

  afterEach(() => vi.useRealTimers());

  it('reads nothing until a picker asks, then one window from 2000 through the end of next year', async () => {
    http.expectNone(isCalendarRead);

    expect(service.sessions()).toBeNull();
    TestBed.tick();
    const read = http.expectOne(isCalendarRead);
    expect(read.request.params.get('start_ms')).toBe(String(etMidnightMs('2000-01-01')));
    expect(read.request.params.get('end_ms')).toBe(String(etMidnightMs('2027-12-31')));
    read.flush(FRIDAY);
    await settle();

    expect(service.sessions()?.isSession('2026-10-02')).toBe(true);
    expect(service.sessions()?.covers('2000-01-03')).toBe(true);
    expect(service.problem()).toBeNull();
    expect(service.lastCompleted()).toBe('2026-10-02');
    // A second reader shares the one read.
    http.verify();
  });

  it('moves the last completed session when a session closes, so an open tab is not left on the day before', async () => {
    // Friday 2026-10-02, noon ET: Thursday has closed, Friday has not.
    vi.setSystemTime(Date.UTC(2026, 9, 2, 16));
    service.sessions();
    TestBed.tick();
    http.expectOne(isCalendarRead).flush({
      session_open_ms: [Date.UTC(2026, 9, 1, 13, 30), Date.UTC(2026, 9, 2, 13, 30)],
      session_close_ms: [Date.UTC(2026, 9, 1, 20), Date.UTC(2026, 9, 2, 20)],
      holidays: [],
    });
    await settle();
    expect(service.lastCompleted()).toBe('2026-10-01');

    vi.advanceTimersByTime(4 * 60 * 60 * 1000);

    expect(service.lastCompleted()).toBe('2026-10-02');
  });

  it('catches up within a minute of waking when the machine slept through a close', async () => {
    vi.setSystemTime(Date.UTC(2026, 9, 2, 16));
    service.sessions();
    TestBed.tick();
    http.expectOne(isCalendarRead).flush({
      session_open_ms: [Date.UTC(2026, 9, 1, 13, 30), Date.UTC(2026, 9, 2, 13, 30)],
      session_close_ms: [Date.UTC(2026, 9, 1, 20), Date.UTC(2026, 9, 2, 20)],
      holidays: [],
    });
    await settle();
    expect(service.lastCompleted()).toBe('2026-10-01');

    // Asleep for ten hours: the clock moves, the timers do not.
    vi.setSystemTime(Date.UTC(2026, 9, 3, 2));
    expect(service.lastCompleted()).toBe('2026-10-01');
    vi.advanceTimersByTime(60_000);

    expect(service.lastCompleted()).toBe('2026-10-02');
  });

  it('says why the calendar could not be read, and reads it again on retry', async () => {
    service.sessions();
    TestBed.tick();
    http.expectOne(isCalendarRead).error(new ProgressEvent('error'), { status: 0 });
    await settle();

    expect(service.sessions()).toBeNull();
    expect(service.problem()).toBe('The data plane did not respond.');

    service.retry();
    TestBed.tick();
    http.expectOne(isCalendarRead).flush(FRIDAY);
    await settle();

    expect(service.problem()).toBeNull();
    expect(service.sessions()?.isSession('2026-10-02')).toBe(true);
  });

  it("gives the server's own answer when the route is not there, as before the data plane restarts", async () => {
    service.sessions();
    TestBed.tick();
    http.expectOne(isCalendarRead).flush({ detail: 'Not Found' }, { status: 404, statusText: 'Not Found' });
    await settle();

    expect(service.problem()).toContain('404 Not Found');
  });
});
