import { Injectable, inject, signal } from '@angular/core';
import type { AuthenticatedSseStatus } from '../../../../services/authenticated-sse-connection';
import {
  adoptVersionedSnapshot,
  openVersionedSnapshotStream,
  type SnapshotStream,
} from '../../../../services/versioned-snapshot-stream';
import type {
  BotPanelLiveSnapshot,
  ChartLiveResolution,
} from './broker-v2-panel.types';
import { BrokerV2PanelService } from './broker-v2-panel.service';
import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';

interface LivePanelRequest {
  readonly broker: string;
  readonly clerkId: string;
  readonly accountId: string;
  readonly sid: string;
  readonly resolution: ChartLiveResolution;
  readonly bindingGeneration?: number | null;
  readonly routingEpoch?: number | null;
}

const FALLBACK_POLL_MS = 5_000;

function isLiveSnapshot(value: unknown): value is BotPanelLiveSnapshot {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<BotPanelLiveSnapshot>;
  return typeof candidate.stream_epoch === 'string'
    && candidate.stream_epoch.length > 0
    && typeof candidate.surface_version === 'number'
    && Number.isInteger(candidate.surface_version)
    && candidate.surface_version >= 0
    && typeof candidate.panel === 'object'
    && candidate.panel !== null
    && typeof candidate.live_chart === 'object'
    && candidate.live_chart !== null;
}

/** Keeps the last complete same-session panel visible across SSE reconnects. */
@Injectable()
export class BotPanelLiveStore {
  private readonly panelService = inject(BrokerV2PanelService);
  private readonly currentSnapshot = signal<BotPanelLiveSnapshot | null>(null);
  private readonly currentStatus = signal<AuthenticatedSseStatus>('closed');
  private readonly currentError = signal<string | null>(null);
  private stream: SnapshotStream | null = null;
  private fallbackTimer: ReturnType<typeof setInterval> | null = null;
  private request: LivePanelRequest | null = null;
  private generation = 0;
  private transactionRequest = 0;
  private selectedRail: BotPanelLiveSnapshot['panel']['rail'] | null = null;

  readonly snapshot = this.currentSnapshot.asReadonly();
  readonly status = this.currentStatus.asReadonly();
  readonly error = this.currentError.asReadonly();

  async start(request: LivePanelRequest): Promise<void> {
    const generation = ++this.generation;
    const identityChanged = this.request !== null && (
      this.request.broker !== request.broker
      || this.request.clerkId !== request.clerkId
      || this.request.accountId !== request.accountId
      || this.request.sid !== request.sid
      || this.request.bindingGeneration !== request.bindingGeneration
      || this.request.routingEpoch !== request.routingEpoch
    );
    if (identityChanged) {
      this.transactionRequest += 1;
      this.selectedRail = null;
      this.currentSnapshot.set(null);
      this.currentError.set(null);
    }
    this.stopTransport();
    this.request = request;
    this.currentStatus.set('connecting');
    try {
      const bootstrap = await this.fetchSnapshot(request);
      if (!this.isCurrent(generation)) return;
      this.adopt(bootstrap);
    } catch (error) {
      if (!this.isCurrent(generation)) return;
      this.currentError.set(error instanceof Error ? error.message : 'Live snapshot is unavailable.');
    }
    if (!this.isCurrent(generation)) return;
    this.openStream(generation, request);
  }

  async refresh(): Promise<void> {
    const request = this.request;
    if (request === null) return;
    const generation = this.generation;
    try {
      const snapshot = await this.fetchSnapshot(request);
      if (!this.isCurrent(generation)) return;
      this.adopt(snapshot);
    } catch (error) {
      if (!this.isCurrent(generation)) return;
      this.currentError.set(error instanceof Error ? error.message : 'Live snapshot refresh failed.');
    }
  }

  async selectTransaction(transactionRef: string): Promise<void> {
    const request = this.request;
    if (request === null) return;
    const generation = this.generation;
    const transactionRequest = ++this.transactionRequest;
    try {
      const panel = await this.panelService.getPanel(
        this.readTarget(request),
        request.sid,
        transactionRef,
      );
      if (!this.isCurrentTransaction(generation, transactionRequest)) return;
      this.selectedRail = panel.rail;
      this.currentSnapshot.update((current) => current === null
        ? current
        : { ...current, panel: { ...current.panel, rail: panel.rail } });
    } catch (error) {
      if (!this.isCurrentTransaction(generation, transactionRequest)) return;
      this.currentError.set(
        error instanceof Error ? error.message : 'Transaction evidence is unavailable.',
      );
    }
  }

  clearSelectedTransaction(): void {
    if (this.selectedRail === null) return;
    this.transactionRequest += 1;
    this.selectedRail = null;
    void this.refresh();
  }

  stop(): void {
    this.generation += 1;
    this.transactionRequest += 1;
    this.request = null;
    this.stopTransport();
    this.currentStatus.set('closed');
  }

  private fetchSnapshot(request: LivePanelRequest): Promise<BotPanelLiveSnapshot> {
    return this.panelService.getLiveSnapshot(
      this.readTarget(request),
      request.sid,
      request.resolution,
    );
  }

  /** The read target every snapshot, panel and stream request is addressed
   * by — broker and clerk identity on the wire (FR-092), late responses for
   * a superseded lane discarded by the identity comparison in start(). */
  private readTarget(request: LivePanelRequest): ResourceTarget {
    return resourceTarget(request.broker, request.clerkId, {
      accountId: request.accountId,
      entityId: request.sid,
      bindingGeneration: request.bindingGeneration ?? null,
      routingEpoch: request.routingEpoch ?? null,
    });
  }

  private openStream(generation: number, request: LivePanelRequest): void {
    this.stream = openVersionedSnapshotStream(
      () => {
        const snapshot = this.currentSnapshot();
        const cursor = snapshot === null
          ? undefined
          : `${snapshot.stream_epoch}:${snapshot.surface_version}`;
        return this.panelService.liveStreamUrl(
          this.readTarget(request),
          request.sid,
          request.resolution,
          cursor,
        );
      },
      isLiveSnapshot,
      'Bot panel stream',
      {
        onSnapshot: (snapshot) => {
          if (this.isCurrent(generation)) this.adopt(snapshot);
        },
        onMalformedSnapshot: (message) => {
          if (this.isCurrent(generation)) this.currentError.set(message);
        },
        onReset: () => {
          if (this.isCurrent(generation)) void this.refresh();
        },
        onStatus: (status) => {
          if (!this.isCurrent(generation)) return;
          this.currentStatus.set(status);
          if (status === 'open' || status === 'closed') this.stopFallback();
          if (status === 'error') this.startFallback();
        },
      },
    );
  }

  private adopt(candidate: BotPanelLiveSnapshot): void {
    if (this.selectedRail !== null) {
      candidate = { ...candidate, panel: { ...candidate.panel, rail: this.selectedRail } };
    }
    const current = this.currentSnapshot();
    const adopted = adoptVersionedSnapshot(current, candidate);
    if (adopted !== current) this.currentSnapshot.set(adopted);
    this.currentError.set(null);
  }

  private isCurrent(generation: number): boolean {
    return generation === this.generation;
  }

  private isCurrentTransaction(generation: number, transactionRequest: number): boolean {
    return this.isCurrent(generation) && transactionRequest === this.transactionRequest;
  }

  private startFallback(): void {
    if (this.fallbackTimer !== null) return;
    this.fallbackTimer = setInterval(() => void this.refresh(), FALLBACK_POLL_MS);
  }

  private stopFallback(): void {
    if (this.fallbackTimer === null) return;
    clearInterval(this.fallbackTimer);
    this.fallbackTimer = null;
  }

  private stopTransport(): void {
    this.stream?.close();
    this.stream = null;
    this.stopFallback();
  }
}
