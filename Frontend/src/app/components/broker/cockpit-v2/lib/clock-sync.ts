// PRD #617 — clock-offset utility.
//
// Captures `serverAsOfMs - clientNow()` on every operator-surface
// response and exposes the current "server time" (`clientNow() + offset`)
// for the identity-strip clock pill.  No accumulated drift — the
// offset is recomputed on every server response, on browser focus,
// and on tab visibility transitions.
//
// The CLOCK DIFFERENCE advisory fires when |offset| > 30_000 ms.  The
// cockpit continues to use server-relative time for transition
// scheduling even when the advisory is active — the warning is
// surface, not a fallback.

const ADVISORY_THRESHOLD_MS = 30_000;

export interface ClockSnapshot {
  offsetMs: number;
  clientNowMs: number;
  serverNowMs: number;
  advisory: boolean;
}

export type ClientNow = () => number;

export class ClockSync {
  private _offsetMs = 0;
  private _hasObservation = false;

  constructor(private readonly _clientNow: ClientNow = () => Date.now()) {}

  /** Capture a new server observation.  Resets accumulated drift. */
  observe(serverAsOfMs: number): void {
    const now = this._clientNow();
    this._offsetMs = serverAsOfMs - now;
    this._hasObservation = true;
  }

  snapshot(): ClockSnapshot {
    const clientNowMs = this._clientNow();
    const serverNowMs = clientNowMs + this._offsetMs;
    return {
      offsetMs: this._offsetMs,
      clientNowMs,
      serverNowMs,
      advisory: this._hasObservation && Math.abs(this._offsetMs) > ADVISORY_THRESHOLD_MS,
    };
  }

  /**
   * Compute the next refresh schedule against the trading-session
   * boundary.  Returns ms-from-now for two timers:
   *   - `earlyMs` — 15 seconds before the boundary (heads-up refresh)
   *   - `boundaryMs` — 1 second after the boundary (catches the new phase)
   * Returns `null` for either timer if it would fall in the past
   * (boundary has already passed; caller waits for the next normal poll).
   */
  scheduleBoundaryRefresh(nextTransitionMs: number | null): {
    earlyMs: number | null;
    boundaryMs: number | null;
  } {
    if (nextTransitionMs === null) {
      return { earlyMs: null, boundaryMs: null };
    }
    const serverNow = this.snapshot().serverNowMs;
    const earlyAt = nextTransitionMs - 15_000;
    const boundaryAt = nextTransitionMs + 1_000;
    const earlyMs = earlyAt > serverNow ? earlyAt - serverNow : null;
    const boundaryMs = boundaryAt > serverNow ? boundaryAt - serverNow : null;
    return { earlyMs, boundaryMs };
  }
}
