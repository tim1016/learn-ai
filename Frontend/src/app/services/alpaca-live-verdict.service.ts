import { DestroyRef, Injectable, inject, signal } from '@angular/core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { resourceTarget } from '../fleet/resource-target';
import { BrokersService } from './brokers.service';

const POLL_INTERVAL_MS = 5000;

/** One lane's latest live-verdict read, or the error from its last attempt. */
export interface LaneVerdictState {
  readonly verdict: AlpacaLiveVerdict | null;
  readonly lastError: unknown;
}

/** What a lane reads as before its first poll response ever lands. */
export const UNPOLLED_LANE_STATE: LaneVerdictState = Object.freeze({
  verdict: null,
  lastError: null,
});

/**
 * Singleton owner of the Alpaca live-verdict signal, keyed by `clerk_id`
 * (ADR 0059 D8; #2110).
 *
 * Polls `GET /api/brokers/alpaca/clerks/{clerk_id}/live-verdict` for every
 * lane `FleetDirectoryService.lanesOf('alpaca')` currently reports, every
 * five seconds, and exposes the latest per-lane state as one signal. The
 * shell renders one badge per lane from it (FR-093: one lane's failed or
 * slow read must never blank, delay, or overwrite another lane's state —
 * this service stores each lane under its own key, so a rejected read for
 * one `clerk_id` only ever overwrites that `clerk_id`'s entry).
 *
 * A single poll timer drives every lane's read, and each read still goes
 * through `BrokersService`'s shared `PolledReadScheduler` — this does not
 * add an independent poller per lane, it fans one 5 s tick out to N reads.
 *
 * Never derive the mode from an env var or an account-id shape here: the
 * server's verdict is the only source of truth (ADR 0011 §7).
 */
@Injectable({ providedIn: 'root' })
export class AlpacaLiveVerdictService {
  private readonly brokers = inject(BrokersService);
  private readonly directory = inject(FleetDirectoryService);
  private readonly destroyRef = inject(DestroyRef);

  private readonly _stateByClerkId = signal<ReadonlyMap<string, LaneVerdictState>>(new Map());
  readonly stateByClerkId = this._stateByClerkId.asReadonly();

  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  constructor() {
    this.destroyRef.onDestroy(() => this.stop());
  }

  /** Begin background polling. Idempotent — the shell calls this once on boot. */
  start(): void {
    if (this.started) return;
    this.started = true;
    void this.refresh();
    this.pollTimer = setInterval(() => void this.refresh(), POLL_INTERVAL_MS);
  }

  /** This lane's latest state, or the unpolled default if it has never
   * reported (not yet in the directory, or polled before this lane existed). */
  stateFor(clerkId: string): LaneVerdictState {
    return this._stateByClerkId().get(clerkId) ?? UNPOLLED_LANE_STATE;
  }

  /** One polling tick across every known Alpaca lane. Each lane's read is
   * independent end to end — its own request, its own try/catch, its own
   * map entry — so one lane's rejection can never touch another's state. */
  async refresh(): Promise<void> {
    const lanes = this.directory.lanesOf('alpaca');
    await Promise.all(lanes.map((lane) => this.refreshLane(lane.broker, lane.clerk_id)));
  }

  private async refreshLane(broker: string, clerkId: string): Promise<void> {
    try {
      const verdict = await this.brokers.getLiveVerdict(resourceTarget(broker, clerkId));
      this.setState(clerkId, { verdict, lastError: null });
    } catch (err) {
      // A failed read means this lane's verdict is unknown to this client;
      // never keep rendering a stale mode over a network fault.
      this.setState(clerkId, { verdict: null, lastError: err });
    }
  }

  private setState(clerkId: string, state: LaneVerdictState): void {
    this._stateByClerkId.update((current) => {
      const next = new Map(current);
      next.set(clerkId, state);
      return next;
    });
  }

  private stop(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.started = false;
  }
}
