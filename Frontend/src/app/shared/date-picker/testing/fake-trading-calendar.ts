import { signal, type Provider, type Signal } from '@angular/core';

import { etMidnightMs } from '../../date/et-midnight';
import { addDays, isWeekend, type IsoDate } from '../../date/iso-date';
import type { DateBounds } from '../date-bounds';
import { ProviderBoundsService } from '../provider-bounds.service';
import { TradingCalendarService } from '../trading-calendar.service';
import { TradingSessions } from '../trading-sessions';

const HOUR_MS = 60 * 60 * 1000;

/**
 * A calendar for specs: every weekday in ``[from, through]`` is a session
 * from 09:30 to 16:00 ET, except the named holidays.
 */
export function fakeSessions(from: IsoDate, through: IsoDate, holidays: Readonly<Record<IsoDate, string>> = {}): TradingSessions {
  const opens: number[] = [];
  const closes: number[] = [];
  for (let day = from; day <= through; day = addDays(day, 1)) {
    if (isWeekend(day) || day in holidays) continue;
    opens.push(etMidnightMs(day) + 9.5 * HOUR_MS);
    closes.push(etMidnightMs(day) + 16 * HOUR_MS);
  }
  const closed = Object.entries(holidays).map(([day, name]) => ({ date_ms: etMidnightMs(day) + 9.5 * HOUR_MS, name }));
  return new TradingSessions({ session_open_ms: opens, session_close_ms: closes, holidays: closed }, from, through);
}

/** The surface of ``TradingCalendarService`` a picker reads, with the clock fixed at ``now``. */
export interface FakeTradingCalendar {
  readonly sessions: Signal<TradingSessions | null>;
  readonly problem: Signal<string | null>;
  retry(): void;
  nowMs(): number;
}

/** A stand-in for ``TradingCalendarService``: ``now`` is noon ET on that date. */
export function fakeTradingCalendar(sessions: TradingSessions | null, now: IsoDate, problem: string | null = null): FakeTradingCalendar {
  return {
    sessions: signal(sessions),
    problem: signal(problem),
    retry: () => undefined,
    nowMs: () => etMidnightMs(now) + 12 * HOUR_MS,
  };
}

let standardSessions: TradingSessions | null = null;

/** The date world a host spec gets unless it asks for another: built once per spec file, it is the same for every test. */
export const STANDARD_DATE_WORLD = {
  /** Monday; the last completed session is Friday 2026-09-18, the standard picker pool's last held day. */
  now: '2026-09-21',
  providerFloor: '2021-09-21',
  get sessions(): TradingSessions {
    standardSessions ??= fakeSessions('2021-01-01', '2027-12-31');
    return standardSessions;
  },
} as const;

/** The provider's bound as ``ProviderBoundsService`` would state it for ``calendar``. */
export function fakeProviderBounds(calendar: FakeTradingCalendar, floor: IsoDate): DateBounds {
  const last = calendar.sessions()?.lastCompleted(calendar.nowMs()) ?? null;
  return {
    min: floor,
    max: last,
    minReason: `The provider serves bars from ${floor}.`,
    maxReason: last === null ? null : `The last completed session is ${last}.`,
    unknown: null,
  };
}

/**
 * Stands in for the two services every date picker reads, so a host spec
 * mounts no HTTP for the trading calendar or the provider's history floor.
 */
export function provideFakeDateWorld(calendar: FakeTradingCalendar = fakeTradingCalendar(STANDARD_DATE_WORLD.sessions, STANDARD_DATE_WORLD.now)): Provider[] {
  return [
    { provide: TradingCalendarService, useValue: calendar },
    { provide: ProviderBoundsService, useValue: { bounds: signal(fakeProviderBounds(calendar, STANDARD_DATE_WORLD.providerFloor)) } },
  ];
}
