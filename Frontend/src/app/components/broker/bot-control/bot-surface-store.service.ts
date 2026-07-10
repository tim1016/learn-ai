import { HttpErrorResponse } from '@angular/common/http';
import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import type {
  LiveInstanceStatus,
  MutationAttemptReceipt,
} from '../../../api/live-instances.types';
import type { AuthenticatedSseStatus } from '../../../services/authenticated-sse-connection';
import { LiveRunsService } from '../../../services/live-runs.service';
import { adoptVersionedSnapshot } from '../../../services/versioned-snapshot-stream';
import { isLiveInstanceStatus } from './lib/bot-surface-snapshot-adapter';
import { openBotSurfaceStream, type BotSurfaceStream } from './lib/bot-surface-stream';

export type BotSurfaceBootstrap =
  | { readonly kind: 'ready'; readonly snapshot: LiveInstanceStatus }
  | { readonly kind: 'missing'; readonly status: 404 | 410 }
  | { readonly kind: 'unreachable'; readonly message: string };

export interface PendingMutationResponse {
  readonly mutation_attempt_id?: string | null;
}

@Injectable()
export class BotSurfaceStore {
  private readonly liveRuns = inject(LiveRunsService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly snapshot = signal<LiveInstanceStatus | null>(null);
  private readonly bootstrapState = signal<BotSurfaceBootstrap | null>(null);
  private readonly connectionState = signal<AuthenticatedSseStatus>('closed');
  private readonly eventAtMs = signal<number | null>(null);
  private readonly receivedAtMs = signal<number | null>(null);
  private readonly streamSnapshotVerified = signal(false);
  private readonly pendingId = signal<string | null>(null);
  private readonly receivedReceipt = signal<MutationAttemptReceipt | null>(null);
  private readonly activeInstance = signal<string | null>(null);
  private bootstrapPromise: Promise<BotSurfaceBootstrap> | null = null;
  private stream: BotSurfaceStream | null = null;

  readonly instanceId = this.activeInstance.asReadonly();
  readonly status = this.snapshot.asReadonly();
  readonly bootstrap = this.bootstrapState.asReadonly();
  readonly streamStatus = this.connectionState.asReadonly();
  readonly lastEventAtMs = this.eventAtMs.asReadonly();
  readonly snapshotReceivedAtMs = this.receivedAtMs.asReadonly();
  readonly pendingAttemptId = this.pendingId.asReadonly();
  readonly latestMutationReceipt = this.receivedReceipt.asReadonly();
  readonly readOnly = computed(
    () =>
      this.snapshot() === null ||
      this.connectionState() !== 'open' ||
      !this.streamSnapshotVerified(),
  );
  readonly errorMessage = computed<string | null>(() => {
    const bootstrap = this.bootstrapState();
    if (bootstrap?.kind === 'unreachable') return bootstrap.message;
    if (this.connectionState() === 'error') {
      return 'Control plane unreachable. Showing the last same-session snapshot read-only while the stream reconnects.';
    }
    return null;
  });

  constructor() {
    this.destroyRef.onDestroy(() => this.close());
  }

  bootstrapInstance(instanceId: string): Promise<BotSurfaceBootstrap> {
    if (this.activeInstance() === instanceId && this.bootstrapPromise !== null) {
      return this.bootstrapPromise;
    }
    if (this.activeInstance() !== instanceId) this.resetForInstance(instanceId);
    const request = this.loadBootstrap(instanceId);
    this.bootstrapPromise = request;
    return request;
  }

  connect(instanceId: string): void {
    if (this.activeInstance() !== instanceId) this.resetForInstance(instanceId);
    this.stream?.close();
    this.stream = openBotSurfaceStream(instanceId, {
      onStatus: (status) => {
        this.connectionState.set(status);
        if (status !== 'open') this.streamSnapshotVerified.set(false);
      },
      onMalformedSnapshot: (message) => {
        this.bootstrapState.set({ kind: 'unreachable', message });
        this.streamSnapshotVerified.set(false);
        this.connectionState.set('error');
      },
      onSnapshot: (candidate) => {
        this.connectionState.set('open');
        this.streamSnapshotVerified.set(true);
        const receivedAtMs = Date.now();
        this.eventAtMs.set(receivedAtMs);
        this.receivedAtMs.set(receivedAtMs);
        const adopted = adoptVersionedSnapshot(this.snapshot(), candidate);
        if (adopted !== this.snapshot()) this.snapshot.set(adopted);
        this.bootstrapState.set({ kind: 'ready', snapshot: adopted });
        this.adoptMutationReceipt(adopted.latest_mutation);
      },
    });
  }

  establishPending(response: PendingMutationResponse): void {
    const attemptId = response.mutation_attempt_id ?? null;
    if (attemptId === null) return;
    if (this.receivedReceipt()?.mutation_attempt_id === attemptId) {
      this.pendingId.set(null);
      return;
    }
    this.pendingId.set(attemptId);
  }

  close(): void {
    this.stream?.close();
    this.stream = null;
  }

  private async loadBootstrap(instanceId: string): Promise<BotSurfaceBootstrap> {
    try {
      const snapshot = await this.liveRuns.getInstanceStatus(instanceId);
      if (!isLiveInstanceStatus(snapshot, instanceId)) {
        throw new Error('Control plane returned a snapshot for a different or invalid bot identity.');
      }
      if (this.activeInstance() !== instanceId) return { kind: 'ready', snapshot };
      this.snapshot.set(snapshot);
      this.receivedAtMs.set(Date.now());
      this.adoptMutationReceipt(snapshot.latest_mutation);
      const result = { kind: 'ready', snapshot } as const;
      this.bootstrapState.set(result);
      return result;
    } catch (error) {
      const status = error instanceof HttpErrorResponse ? error.status : 0;
      if (status === 404 || status === 410) {
        const result = { kind: 'missing', status } as const;
        this.bootstrapState.set(result);
        return result;
      }
      const result = {
        kind: 'unreachable',
        message: 'Control plane unreachable. Current bot state is unavailable.',
      } as const;
      this.bootstrapState.set(result);
      return result;
    }
  }

  private adoptMutationReceipt(receipt: MutationAttemptReceipt | null): void {
    if (receipt === null) return;
    this.receivedReceipt.set(receipt);
    if (receipt.mutation_attempt_id === this.pendingId()) this.pendingId.set(null);
  }

  private resetForInstance(instanceId: string): void {
    this.close();
    this.activeInstance.set(instanceId);
    this.bootstrapPromise = null;
    this.snapshot.set(null);
    this.bootstrapState.set(null);
    this.connectionState.set('closed');
    this.eventAtMs.set(null);
    this.receivedAtMs.set(null);
    this.streamSnapshotVerified.set(false);
    this.pendingId.set(null);
    this.receivedReceipt.set(null);
  }
}
