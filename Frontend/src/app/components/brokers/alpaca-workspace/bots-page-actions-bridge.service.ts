import { Injectable, signal, type Signal } from '@angular/core';

/**
 * What the Bots tab hands the workspace header's action slot while it is the
 * one mounted: its own refresh/archive commands and their busy state, so the
 * header can render them without owning the roster's fleet read itself.
 */
export interface BotsPageActionsHost {
  readonly refreshing: Signal<boolean>;
  readonly initialLoading: Signal<boolean>;
  readonly refresh: () => void;
  readonly openArchive: () => void;
}

/**
 * The seam between the account workspace header's action slot and the Bots
 * tab, whichever account is routed. The header renders Refresh and Archive
 * finished only while a host is registered — every other tab leaves the slot
 * empty — and `register()` returns the cleanup function `BotsListPageComponent`
 * calls on destroy, the same single-active-host pattern as
 * `ActiveLensBridgeService`.
 */
@Injectable({ providedIn: 'root' })
export class BotsPageActionsBridgeService {
  private readonly _host = signal<BotsPageActionsHost | null>(null);
  readonly host = this._host.asReadonly();

  register(host: BotsPageActionsHost): () => void {
    this._host.set(host);
    return () => {
      if (this._host() === host) this._host.set(null);
    };
  }
}
