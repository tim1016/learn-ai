import { Injectable, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, type Params } from '@angular/router';

import type { ChainMode } from './chain-modes';
import { readChainRequest, type ChainKey } from './chain-request';

/**
 * The chain page's one reader of its address and one writer of it.
 *
 * The address is the truth: every pick navigates, and the request follows the address, so Back,
 * Forward and a reload land on what was asked. A write merges into the query, so it touches the
 * keys it names and leaves every other one (the instrument's window, timeframe and session
 * above all) exactly as it was. Moving an option date therefore cannot move the chart's window.
 *
 * Provided by the chain page, so the source adapters under it share one instance.
 */
@Injectable()
export class ChainRequestService {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly query = toSignal(this.route.queryParamMap, { initialValue: this.route.snapshot.queryParamMap });

  /** What the address asks the chain for. */
  readonly request = computed(() => readChainRequest(this.query()));

  /** The keys the address holds that cannot be read as they stand, each to be dropped (`null`); none when the address is usable. */
  private readonly unusable = computed<Params>(() => {
    const request = this.request();
    return request.kind === 'invalid' ? Object.fromEntries(request.unusable.map((key: ChainKey) => [key, null])) : {};
  });

  /**
   * Chooses a mode. The expiry stays, so the contract is the same one under another source;
   * leaving the archive puts its date and instant away, since no other source has either.
   * A key that cannot be read as it stands (a mistyped expiry, an instant off its session) goes,
   * so choosing a mode is always a way out of an address the chain cannot read.
   */
  chooseMode(mode: ChainMode): void {
    if (mode.unavailable !== null) return;
    this.write({ ...this.unusable(), ...(mode.source === 'archive' ? { source: 'archive' } : { source: mode.source, date: null, at: null }) });
  }

  /** Drops the keys that cannot be read, and nothing else; the source, if it was readable, stays. */
  clearUnusable(): void {
    this.write(this.unusable());
  }

  pickExpiry(expiryMs: number): void {
    this.write({ expiry: String(expiryMs) });
  }

  /**
   * Writes the expiry a chain opened on over the address, with no history entry of its own: the
   * address then says which expiry is on screen, and Back leaves the page instead of undoing it.
   */
  adoptExpiry(expiryMs: number): void {
    this.write({ expiry: String(expiryMs) }, true);
  }

  /** Picks the session date, and the instant the chain is read at on it (`null`: the session's close). */
  pickDate(date: string, atMs: number | null): void {
    this.write({ date, at: atMs === null ? null : String(atMs) });
  }

  /** Picks the instant within the session date (`null`: the session's close). */
  pickTime(atMs: number | null): void {
    this.write({ at: atMs === null ? null : String(atMs) });
  }

  private write(patch: Params, replaceUrl = false): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams: patch, queryParamsHandling: 'merge', replaceUrl });
  }
}
