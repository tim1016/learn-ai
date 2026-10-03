import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ApplicationRef } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
    vi.spyOn(service, 'nowMs').mockReturnValue(Date.UTC(2026, 9, 3, 16));
  });

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
    // A second reader shares the one read.
    expect(service.sessions()?.lastCompleted(Date.UTC(2026, 9, 3, 16))).toBe('2026-10-02');
    http.verify();
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
});
