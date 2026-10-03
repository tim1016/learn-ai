import { HttpClient } from '@angular/common/http';
import { Injectable, Injector, computed, inject, resource, signal, untracked, type ResourceRef } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../environments/environment';
import { classifyDataLakeError } from '../data-lake';
import { etIsoDate, etMidnightMs } from '../date/et-midnight';
import type { IsoDate } from '../date/iso-date';
import { TradingSessions, type TradingCalendarWindow } from './trading-sessions';

/** The first day the calendar is read for: older than any bar the provider serves. */
const CALENDAR_FROM: IsoDate = '2000-01-01';

/**
 * The longest the clock goes unread while a close is awaited. A browser timer
 * does not count the time a machine sleeps, so one timer set for the close
 * itself would fire hours late on a laptop that slept overnight.
 */
const CLOSE_WATCH_MS = 60_000;

type CalendarRead = { readonly kind: 'ok'; readonly sessions: TradingSessions } | { readonly kind: 'failed'; readonly message: string };

/**
 * The trading calendar every date picker shares, read once per tab from the
 * data plane's canonical calendar (ADR 0022). The browser keeps no calendar
 * of its own: when the read fails, pickers say so and offer a retry instead
 * of guessing which days trade.
 */
@Injectable({ providedIn: 'root' })
export class TradingCalendarService {
  private readonly http = inject(HttpClient);
  private readonly injector = inject(Injector);
  private opened: ResourceRef<CalendarRead | undefined> | null = null;
  /** Bumped each time a session closes, so what depends on the clock is worked out again. */
  private readonly closes = signal(0);
  private closeWatch: ReturnType<typeof setTimeout> | null = null;

  /**
   * The one read, started by the first picker that asks. A date field that
   * takes any day never asks, so it costs no request. ``resource()`` installs
   * an effect, which a reactive context forbids, hence ``untracked``.
   */
  private get read(): ResourceRef<CalendarRead | undefined> {
    this.opened ??= untracked(() => resource({ loader: () => this.load(), injector: this.injector }));
    return this.opened;
  }

  /** The calendar; null while it is being read and after a failed read. */
  readonly sessions = computed<TradingSessions | null>(() => {
    const read = this.read.value();
    return read?.kind === 'ok' ? read.sessions : null;
  });

  /** Why the calendar could not be read; null while it loads and once it has. */
  readonly problem = computed<string | null>(() => {
    const read = this.read.value();
    return read?.kind === 'failed' ? read.message : null;
  });

  /**
   * The last session that has closed. It moves when a session closes, so a
   * tab left open across the close offers the day that just finished.
   */
  readonly lastCompleted = computed<IsoDate | null>(() => {
    this.closes();
    return this.sessions()?.lastCompleted(this.nowMs()) ?? null;
  });

  retry(): void {
    this.read.reload();
  }

  /** The current instant. Read through here so a spec can fix the clock. */
  nowMs(): number {
    return Date.now();
  }

  private async load(): Promise<CalendarRead> {
    const through: IsoDate = `${Number(etIsoDate(this.nowMs()).slice(0, 4)) + 1}-12-31`;
    try {
      const window = await firstValueFrom(
        this.http.get<TradingCalendarWindow>(`${environment.pythonServiceUrl}/api/calendar/sessions`, {
          params: { start_ms: etMidnightMs(CALENDAR_FROM), end_ms: etMidnightMs(through) },
        }),
      );
      const sessions = new TradingSessions(window, CALENDAR_FROM, through);
      this.watchForClose(sessions);
      return { kind: 'ok', sessions };
    } catch (error) {
      return { kind: 'failed', message: classifyDataLakeError(error).message };
    }
  }

  /** Counts each scheduled close as the clock passes it, reading the clock at most ``CLOSE_WATCH_MS`` apart. */
  private watchForClose(sessions: TradingSessions, close: number | null = sessions.nextCloseAfter(this.nowMs())): void {
    if (this.closeWatch !== null) clearTimeout(this.closeWatch);
    if (close === null) return;
    this.closeWatch = setTimeout(
      () => {
        if (this.nowMs() < close) {
          this.watchForClose(sessions, close);
          return;
        }
        this.closes.update((count) => count + 1);
        this.watchForClose(sessions);
      },
      Math.min(close - this.nowMs(), CLOSE_WATCH_MS),
    );
  }
}
