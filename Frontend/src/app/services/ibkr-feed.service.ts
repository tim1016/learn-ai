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

  /** One read of every lane. A lane whose read fails is `unknown`, never connected. */
  async refresh(): Promise<void> {
    const lanes = this.directory.lanesOf('alpaca');
    const reads = this.brokers.getClerkStatuses(
      lanes.map((lane) => resourceTarget(lane.broker, lane.clerk_id)),
    );
    const states = await Promise.all(
      reads.map((read) => read.then(laneFeedState, (): LaneFeedState => ({ kind: 'unknown' }))),
    );
    this._stateByClerkId.set(new Map(lanes.map((lane, index) => [lane.clerk_id, states[index]])));
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
