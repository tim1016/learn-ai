import { signal, type Signal } from '@angular/core';

import { etMidnightMs } from '../../date/et-midnight';
import { addDays, isWeekend, type IsoDate } from '../../date/iso-date';
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
