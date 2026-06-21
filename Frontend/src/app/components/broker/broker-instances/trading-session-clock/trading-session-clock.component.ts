import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';

import type {
  OperatorSurfaceTradingSession,
  TradingSessionPhase,
} from '../../../../api/live-instances.types';

const PHASE_LABEL: Record<TradingSessionPhase, string> = {
  PRE: 'PRE',
  RTH: 'RTH',
  POST: 'POST',
  CLOSED: 'CLOSED',
  UNKNOWN: '—',
};

/**
 * Trading-session clock pill for the sticky banner.
 *
 * Server-authored: phase, permission-to-trade, transition boundary,
 * timezone.  Angular ONLY advances the visible HH:MM:SS string from
 * its local wall clock.  Hard-coding RTH or any session policy in
 * Angular is forbidden.
 */
@Component({
  selector: 'app-trading-session-clock',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './trading-session-clock.component.html',
  styleUrl: './trading-session-clock.component.scss',
})
export class TradingSessionClockComponent {
  readonly session = input.required<OperatorSurfaceTradingSession>();

  private readonly _nowMs = signal<number>(Date.now());

  constructor() {
    const handle = setInterval(() => this._nowMs.set(Date.now()), 1000);
    inject(DestroyRef).onDestroy(() => clearInterval(handle));
  }

  readonly phase = computed<TradingSessionPhase>(() => this.session().phase);
  readonly phaseLabel = computed<string>(() => PHASE_LABEL[this.phase()]);

  readonly permits = computed<boolean | null>(
    () => this.session().permits_strategy_activity,
  );

  /**
   * HH:MM:SS in the server-authored timezone.  Falls back to UTC if
   * the timezone string is unrecognized by Intl (defensive only —
   * server emits IANA names).
   */
  readonly clockText = computed<string>(() => {
    const tz = this.session().timezone || 'UTC';
    try {
      return new Intl.DateTimeFormat('en-US', {
        timeZone: tz,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hourCycle: 'h23',
      }).format(this._nowMs());
    } catch {
      return new Intl.DateTimeFormat('en-US', {
        timeZone: 'UTC',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hourCycle: 'h23',
      }).format(this._nowMs());
    }
  });

  readonly tone = computed<'ok' | 'muted' | 'unknown'>(() => {
    if (this.permits() === true) return 'ok';
    if (this.permits() === false) return 'muted';
    return 'unknown';
  });
}
