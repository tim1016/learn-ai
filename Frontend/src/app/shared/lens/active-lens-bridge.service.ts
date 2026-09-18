import { Injectable, signal, type Signal } from '@angular/core';

import type { DeskLens } from './lens';

/**
 * What a lens-owning page hands the global top bar while it is the one
 * mounted: its own `lens` signal and change handler (so switching still runs
 * that page's own side effects and persistence — query param, localStorage,
 * whatever it already does), plus the DOM-id wiring `app-lens-tabs` needs to
 * point `aria-controls` at that page's own panel elements.
 */
export interface ActiveLensHost {
  readonly lens: Signal<DeskLens>;
  readonly select: (lens: DeskLens) => void;
  readonly ariaLabel?: string;
  readonly idPrefix?: string;
  readonly panelId?: string | null;
}

/**
 * The seam between the global top bar's one Trader/Operator toggle and
 * whichever lens-owning page (the full bot panel, the Alpaca account desk,
 * the triage detail) is currently mounted. Routes are mutually exclusive and
 * the triage detail is a modal over its own route, so at most one host is
 * ever registered; the top bar renders nothing when none is.
 *
 * A host registers only while it has lens-bearing content to show (not
 * during its own loading or empty states) and unregisters on destroy —
 * `register()` returns the cleanup function for exactly that.
 */
@Injectable({ providedIn: 'root' })
export class ActiveLensBridgeService {
  private readonly _host = signal<ActiveLensHost | null>(null);
  readonly host = this._host.asReadonly();

  register(host: ActiveLensHost): () => void {
    this._host.set(host);
    return () => {
      if (this._host() === host) this._host.set(null);
    };
  }
}
