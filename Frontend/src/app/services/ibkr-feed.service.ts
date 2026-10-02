import { DestroyRef, Injectable, inject, signal } from '@angular/core';

import type { ClerkStatus } from '../api/alpaca.types';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { resourceTarget } from '../fleet/resource-target';
import { BrokersService } from './brokers.service';

/** The Clerk samples its channels every 15 s (`HOLD_SYNC_INTERVAL_S`), so a
 * faster read would show nothing new. */
const POLL_INTERVAL_MS = 15_000;

/** One lane's IBKR market-data connection, as its Clerk last reported it. */
export type LaneFeedState =
  | { readonly kind: 'connected' }
  | { readonly kind: 'disconnected'; readonly reason: string; readonly sinceMs: number | null }
  | { readonly kind: 'unknown' };

/** Read the market-data channel out of one Clerk status.
 *
 * Connected-but-not-ready is a symbol warming up, not an outage, so it reads
 * as connected. `sinceMs` is when the Clerk's stream-health hold began
 * refusing entries; the channel's own observation time is re-stamped on
 * every sample and cannot date the outage. */
export function laneFeedState(status: ClerkStatus): LaneFeedState {
  const marketData = status.channel_healths?.find((channel) => channel.stream === 'market_data');
  if (marketData === undefined) return { kind: 'unknown' };
  if (marketData.connected) return { kind: 'connected' };
  const streamHold = status.hold.active && status.hold.reason_code === 'STREAM_HEALTH_HOLD';
  return {
    kind: 'disconnected',
    reason: marketData.reason ?? '',
    sinceMs: streamHold ? (status.hold.since_ms ?? null) : null,
  };
}

/**
 * Polls every Alpaca lane's Clerk status for the shell's IBKR data pill.
 *
 * Every Clerk reads its bars through the one IB Gateway, so when Gateway logs
 * out overnight all lanes refuse entries and deploys at once. The roster is
 * whatever `FleetDirectoryService` currently knows; the live-verdict poller
 * keeps it loaded.
 */
@Injectable({ providedIn: 'root' })
export class IbkrFeedService {
  private readonly brokers = inject(BrokersService);
  private readonly directory = inject(FleetDirectoryService);

  private readonly _stateByClerkId = signal<ReadonlyMap<string, LaneFeedState>>(new Map());
  readonly stateByClerkId = this._stateByClerkId.asReadonly();

  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;

  constructor() {
    inject(DestroyRef).onDestroy(() => this.stop());
  }

  /** Begin background polling. Idempotent — the shell calls this once on boot. */
  start(): void {
    if (this.started) return;
    this.started = true;
    this.runTick();
  }

  /** One read of every lane, each published as it settles so a slow lane
   * never hides a fast lane's outage. A failed read is `unknown`, never
   * connected. */
  async refresh(): Promise<void> {
    try {
      // Joins the shell's in-flight roster load on a cold boot, so the first
      // tick reads the lanes instead of an empty roster 15 s too early.
      await this.directory.ensureLoaded();
    } catch {
      // Handled where it belongs: FleetDirectoryService owns this error and
      // the shell's lane badge renders an unresolved roster loudly. This
      // tick reads whatever lanes the directory last knew.
    }
    const lanes = this.directory.lanesOf('alpaca');
    const known = new Set(lanes.map((lane) => lane.clerk_id));
    this._stateByClerkId.update(
      (current) => new Map([...current].filter(([clerkId]) => known.has(clerkId))),
    );
    const reads = this.brokers.getClerkStatuses(
      lanes.map((lane) => resourceTarget(lane.broker, lane.clerk_id)),
    );
    await Promise.all(
      lanes.map((lane, index) =>
        reads[index]
          .then(laneFeedState, (): LaneFeedState => ({ kind: 'unknown' }))
          .then((state) => this.setState(lane.clerk_id, state)),
      ),
    );
  }

  private setState(clerkId: string, state: LaneFeedState): void {
    this._stateByClerkId.update((current) => new Map(current).set(clerkId, state));
  }

  private runTick(): void {
    void this.refresh().finally(() => {
      if (this.started) this.pollTimer = setTimeout(() => this.runTick(), POLL_INTERVAL_MS);
    });
  }

  private stop(): void {
    if (this.pollTimer !== null) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    this.started = false;
  }
}
