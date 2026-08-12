import { HttpClient } from '@angular/common/http';
import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import {
  openAuthenticatedSseConnection,
  type AuthenticatedSseConnection,
  type AuthenticatedSseStatus,
} from '../../../../../services/authenticated-sse-connection';
import type { ChartBar, ChartFillMarker } from '../../lib/broker-v2-panel.types';
import type {
  GalleryBotView,
  GalleryLiveSnapshot,
  GalleryLiveStatus,
  GalleryLiveUpdate,
} from './gallery.types';

const FALLBACK_POLL_MS = 5_000;

interface GalleryRequest {
  readonly broker: string;
  readonly accountId: string;
}

function isGalleryLiveSnapshot(value: unknown): value is GalleryLiveSnapshot {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<GalleryLiveSnapshot>;
  return typeof candidate.stream_epoch === 'string'
    && candidate.stream_epoch.length > 0
    && typeof candidate.surface_version === 'number'
    && Number.isInteger(candidate.surface_version)
    && Array.isArray(candidate.bots)
    && Array.isArray(candidate.symbols)
    && typeof candidate.markers === 'object'
    && candidate.markers !== null;
}

function isGalleryLiveUpdate(value: unknown): value is GalleryLiveUpdate {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<GalleryLiveUpdate>;
  return typeof candidate.surface_version === 'number'
    && Number.isInteger(candidate.surface_version)
    && Array.isArray(candidate.symbols)
    && Array.isArray(candidate.bots_delta)
    && Array.isArray(candidate.removed_sids)
    && typeof candidate.markers_delta === 'object'
    && candidate.markers_delta !== null;
}

/** Replace the bar sharing a ``start_ms`` (the forming bar); otherwise append. Always returns ``start_ms``-sorted. */
function mergeBarsBySymbol(existing: readonly ChartBar[], incoming: readonly ChartBar[]): ChartBar[] {
  if (incoming.length === 0) return [...existing];
  const byStart = new Map(existing.map((bar) => [bar.start_ms, bar]));
  for (const bar of incoming) byStart.set(bar.start_ms, bar);
  return [...byStart.values()].sort((a, b) => a.start_ms - b.start_ms);
}

/** Replace the marker sharing an ``order_ref`` in place; otherwise append. */
function mergeMarkersByRef(
  existing: readonly ChartFillMarker[],
  incoming: readonly ChartFillMarker[],
): ChartFillMarker[] {
  if (incoming.length === 0) return [...existing];
  const byRef = new Map(existing.map((marker) => [marker.order_ref, marker]));
  for (const marker of incoming) byRef.set(marker.order_ref, marker);
  return [...byRef.values()];
}

function upsertBots(existing: readonly GalleryBotView[], deltas: readonly GalleryBotView[]): GalleryBotView[] {
  if (deltas.length === 0) return [...existing];
  const bySid = new Map(existing.map((bot) => [bot.sid, bot]));
  for (const delta of deltas) bySid.set(delta.sid, delta);
  return [...bySid.values()];
}

function dropRemoved(bots: readonly GalleryBotView[], removedSids: readonly string[]): GalleryBotView[] {
  if (removedSids.length === 0) return [...bots];
  const removed = new Set(removedSids);
  return bots.filter((bot) => !removed.has(bot.sid));
}

/**
 * Component-provided live store for the aggregated bot gallery wall
 * (``GET/SSE .../gallery/{snapshot,stream}``, one Alpaca account, all
 * running bots + their shared per-symbol bars).
 *
 * EventSource lifecycle is modeled on ``AccountDeskHoldingsStore``
 * (auto-close on host destroy via ``DestroyRef``) using the lower-level
 * ``openAuthenticatedSseConnection`` primitive directly — the gallery
 * stream has two named event types (``snapshot`` primary, ``update`` and
 * ``reset`` as control events), which that primitive already supports,
 * rather than the single-event ``openVersionedSnapshotStream`` wrapper
 * ``BotPanelLiveStore`` uses. The 5s poll-fallback-on-error behavior is
 * mirrored from ``BotPanelLiveStore``.
 *
 * The wire-frame parsing/merge logic is exposed as ``ingestSnapshot`` /
 * ``ingestUpdate`` so it is directly unit-testable without a live
 * ``EventSource`` (jsdom has none) — the SSE handlers below only parse
 * and validate the raw frame, then route into these.
 */
@Injectable()
export class GalleryLiveStore {
  private readonly http = inject(HttpClient);
  private readonly destroyRef = inject(DestroyRef);

  private readonly botsState = signal<GalleryBotView[]>([]);
  private readonly barsState = signal<ReadonlyMap<string, ChartBar[]>>(new Map());
  private readonly markersState = signal<ReadonlyMap<string, ChartFillMarker[]>>(new Map());
  private readonly statusState = signal<GalleryLiveStatus>('connecting');

  readonly bots = this.botsState.asReadonly();
  readonly barsBySymbol = this.barsState.asReadonly();
  readonly markersBySid = this.markersState.asReadonly();
  readonly status = this.statusState.asReadonly();

  private request: GalleryRequest | null = null;
  private connection: AuthenticatedSseConnection | null = null;
  private fallbackTimer: ReturnType<typeof setInterval> | null = null;
  private generation = 0;
  // Empty string == "no snapshot adopted yet"; also doubles as the
  // ``ingestUpdate``/``applyTransportStatus`` "do we have data" guard.
  private epoch = '';
  private surfaceVersion = -1;

  constructor() {
    this.destroyRef.onDestroy(() => this.stop());
  }

  async start(broker: string, accountId: string): Promise<void> {
    const identityChanged = this.request !== null
      && (this.request.broker !== broker || this.request.accountId !== accountId);
    this.closeTransport();
    const request: GalleryRequest = { broker, accountId };
    this.request = request;
    const generation = ++this.generation;
    if (identityChanged) {
      // A different account's tiles must never linger under the new
      // identity while the fresh bootstrap is in flight — clear
      // immediately, synchronously. A same-identity restart (e.g. a
      // caller-driven reconnect) intentionally skips this so the wall
      // does not flash empty on every reconnect.
      this.epoch = '';
      this.surfaceVersion = -1;
      this.botsState.set([]);
      this.barsState.set(new Map());
      this.markersState.set(new Map());
    }
    this.statusState.set('connecting');
    await this.bootstrap(generation, request);
    if (generation !== this.generation) return;
    this.openStream(generation, request);
  }

  stop(): void {
    this.generation += 1;
    this.request = null;
    this.closeTransport();
    this.statusState.set('connecting');
  }

  /** Full replace — snapshot semantics. Ignored if it is not newer than what is already adopted. */
  ingestSnapshot(snapshot: GalleryLiveSnapshot): void {
    if (this.epoch === snapshot.stream_epoch && snapshot.surface_version <= this.surfaceVersion) return;
    this.epoch = snapshot.stream_epoch;
    this.surfaceVersion = snapshot.surface_version;
    this.botsState.set([...snapshot.bots]);
    this.barsState.set(new Map(snapshot.symbols.map((entry) => [entry.symbol, [...entry.bars]])));
    this.markersState.set(
      new Map(Object.entries(snapshot.markers).map(([sid, markers]) => [sid, [...markers]])),
    );
  }

  /** Incremental merge — per-symbol bars, per-sid markers, bot upserts/removals. */
  ingestUpdate(update: GalleryLiveUpdate): void {
    // No snapshot adopted yet, or a stale/duplicate/out-of-order frame
    // (possible if the bootstrap fetch and a live-stream update race).
    if (this.epoch === '' || update.surface_version <= this.surfaceVersion) return;
    this.surfaceVersion = update.surface_version;

    if (update.symbols.length > 0) {
      this.barsState.update((current) => {
        const next = new Map(current);
        for (const entry of update.symbols) {
          next.set(entry.symbol, mergeBarsBySymbol(current.get(entry.symbol) ?? [], entry.bars));
        }
        return next;
      });
    }

    const markerEntries = Object.entries(update.markers_delta);
    if (markerEntries.length > 0) {
      this.markersState.update((current) => {
        const next = new Map(current);
        for (const [sid, markers] of markerEntries) {
          next.set(sid, mergeMarkersByRef(current.get(sid) ?? [], markers));
        }
        return next;
      });
    }

    if (update.bots_delta.length > 0 || update.removed_sids.length > 0) {
      this.botsState.update((current) =>
        dropRemoved(upsertBots(current, update.bots_delta), update.removed_sids));
    }
  }

  private async bootstrap(generation: number, request: GalleryRequest): Promise<void> {
    try {
      const snapshot = await firstValueFrom(
        this.http.get<GalleryLiveSnapshot>(this.snapshotUrl(request)),
      );
      if (generation !== this.generation) return;
      this.ingestSnapshot(snapshot);
    } catch {
      // The live stream (opened by the caller regardless of this
      // outcome) is the primary transport; a failed bootstrap fetch just
      // forgoes the fast first paint. `applyTransportStatus` and the
      // error-triggered fallback poll below are the recovery path — not
      // a silent swallow, since the resulting status is user-visible.
    }
  }

  private openStream(generation: number, request: GalleryRequest): void {
    this.connection = openAuthenticatedSseConnection(
      () => this.streamUrl(request),
      'snapshot',
      {
        onStatus: (status) => {
          if (generation !== this.generation) return;
          this.applyTransportStatus(status, generation, request);
        },
        onEvent: (event) => {
          if (generation !== this.generation) return;
          this.parseAndIngest(event.data, isGalleryLiveSnapshot, (snapshot) => this.ingestSnapshot(snapshot));
        },
        onControlEvent: (name, event) => {
          if (generation !== this.generation) return;
          if (name === 'update') {
            this.parseAndIngest(event.data, isGalleryLiveUpdate, (update) => this.ingestUpdate(update));
          } else if (name === 'reset') {
            void this.bootstrap(generation, request);
          }
        },
      },
      ['update', 'reset'],
    );
  }

  private parseAndIngest<T>(
    raw: string,
    isValid: (value: unknown) => value is T,
    ingest: (value: T) => void,
  ): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      this.statusState.set('error');
      return;
    }
    if (!isValid(parsed)) {
      this.statusState.set('error');
      return;
    }
    ingest(parsed);
  }

  private applyTransportStatus(
    status: AuthenticatedSseStatus,
    generation: number,
    request: GalleryRequest,
  ): void {
    if (status === 'connecting') {
      // Only the very first connect is worth surfacing as 'connecting' —
      // once we hold a snapshot, a reconnect attempt should read as
      // 'stale' (set by the 'error' branch below), not regress the UI
      // back to a blank "connecting" state.
      if (this.epoch === '') this.statusState.set('connecting');
      return;
    }
    if (status === 'open') {
      this.statusState.set('live');
      this.stopFallback();
      return;
    }
    if (status === 'error') {
      this.statusState.set(this.epoch === '' ? 'error' : 'stale');
      this.startFallback(generation, request);
    }
  }

  private startFallback(generation: number, request: GalleryRequest): void {
    if (this.fallbackTimer !== null) return;
    this.fallbackTimer = setInterval(() => void this.bootstrap(generation, request), FALLBACK_POLL_MS);
  }

  private stopFallback(): void {
    if (this.fallbackTimer === null) return;
    clearInterval(this.fallbackTimer);
    this.fallbackTimer = null;
  }

  private closeTransport(): void {
    this.connection?.close();
    this.connection = null;
    this.stopFallback();
  }

  private base(request: GalleryRequest): string {
    return `/api/brokers/${encodeURIComponent(request.broker)}/accounts/${encodeURIComponent(request.accountId)}`;
  }

  private snapshotUrl(request: GalleryRequest): string {
    return `${this.base(request)}/gallery/snapshot`;
  }

  private streamUrl(request: GalleryRequest): string {
    const params = new URLSearchParams();
    if (this.epoch !== '') params.set('cursor', `${this.epoch}:${this.surfaceVersion}`);
    const query = params.toString();
    return `${this.base(request)}/gallery/stream${query ? `?${query}` : ''}`;
  }
}
