import { CommonModule } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  computed,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { ButtonModule } from 'primeng/button';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type {
  BrokerSessionAttentionCode,
  BrokerSessionEvent,
  BrokerSessionIdentityType,
  BrokerSessionMirrorSnapshot,
  BrokerSessionRecency,
  BrokerSessionRecoveryState,
  BrokerSessionRosterRow,
} from '../../../api/broker-session-mirror.types';
import {
  BROKER_SESSION_PURGE_CONFIRM,
  type BrokerSessionEventPurgeRequest,
} from '../../../api/broker-session-mirror.types';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import { BrokerSessionMirrorService } from '../../../services/broker-session-mirror.service';
import { fmtInteger, fmtTimestampNy } from '../format';
import { BrokerSessionEventsPanelComponent } from './broker-session-events-panel.component';

type TagSeverity = 'success' | 'info' | 'warn' | 'danger' | 'secondary';

interface MirrorSummary {
  current: number;
  past: number;
  unknown: number;
  attention: number;
}

@Component({
  selector: 'app-broker-session-mirror',
  imports: [
    CommonModule,
    ButtonModule,
    BrokerSessionEventsPanelComponent,
    TableModule,
    TagModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-session-mirror.component.html',
  styleUrl: './broker-session-mirror.component.scss',
})
export class BrokerSessionMirrorComponent {
  private readonly injector = inject(Injector);
  private readonly mirror = inject(BrokerSessionMirrorService);
  private readonly router = inject(Router);
  private readonly snapshotStream: SseStream<BrokerSessionMirrorSnapshot> =
    runInInjectionContext(this.injector, () =>
      brokerSse<BrokerSessionMirrorSnapshot>(
        '/api/broker/session-mirror/stream',
        'snapshot',
        { maxBuffer: 1 },
      ),
    );
  private readonly eventStream: SseStream<BrokerSessionEvent> =
    runInInjectionContext(this.injector, () =>
      brokerSse<BrokerSessionEvent>(
        '/api/broker/session-mirror/events/stream',
        'broker_event',
        { maxBuffer: 500 },
      ),
    );

  readonly manualSnapshot = signal<BrokerSessionMirrorSnapshot | null>(null);
  readonly isRefreshing = signal<boolean>(false);
  readonly refreshError = signal<string | null>(null);
  readonly purgeClientIdText = signal<string>('');
  readonly purgeStartMsText = signal<string>('');
  readonly purgeEndMsText = signal<string>('');
  readonly purgeConfirmText = signal<string>('');
  readonly isPurging = signal<boolean>(false);
  readonly purgeMessage = signal<string | null>(null);
  readonly purgeError = signal<string | null>(null);
  readonly purgeConfirmToken = BROKER_SESSION_PURGE_CONFIRM;

  readonly snapshot = computed<BrokerSessionMirrorSnapshot | null>(
    () => this.snapshotStream.latest() ?? this.manualSnapshot(),
  );
  readonly rows = computed<BrokerSessionRosterRow[]>(
    () => this.snapshot()?.rows ?? [],
  );
  readonly summary = computed<MirrorSummary>(() => summarizeRows(this.rows()));
  readonly streamStatus = this.snapshotStream.status;
  readonly streamError = this.snapshotStream.lastError;
  readonly eventStreamStatus = this.eventStream.status;
  readonly eventStreamError = this.eventStream.lastError;
  readonly events = this.eventStream.data;
  readonly canPurge = computed<boolean>(
    () => this.buildPurgeRequest() !== null && !this.isPurging(),
  );

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    this.isRefreshing.set(true);
    this.refreshError.set(null);
    try {
      this.manualSnapshot.set(await this.mirror.snapshot());
    } catch (err) {
      this.refreshError.set(humanError(err));
    } finally {
      this.isRefreshing.set(false);
    }
  }

  async openBot(row: BrokerSessionRosterRow): Promise<void> {
    if (!row.strategy_instance_id) return;
    await this.router.navigate(['/broker/bots', row.strategy_instance_id]);
  }

  async purgeEvents(): Promise<void> {
    const request = this.buildPurgeRequest();
    if (request === null) return;
    this.isPurging.set(true);
    this.purgeError.set(null);
    this.purgeMessage.set(null);
    try {
      const result = await this.mirror.purgeEvents(request);
      this.purgeConfirmText.set('');
      this.eventStream.clear();
      this.purgeMessage.set(
        `Purged ${this.formatNumber(result.purged_count)} events; ${this.formatNumber(result.remaining_count)} remain.`,
      );
      await this.refresh();
    } catch (err) {
      this.purgeError.set(humanError(err));
    } finally {
      this.isPurging.set(false);
    }
  }

  async runNoticeAction(row: BrokerSessionRosterRow): Promise<void> {
    if (row.notice?.action.kind !== 'focus_cockpit_action') return;
    await this.openBot(row);
  }

  noticeActionDisabled(row: BrokerSessionRosterRow): boolean {
    return (
      row.notice?.action.kind !== 'focus_cockpit_action' ||
      row.strategy_instance_id === null
    );
  }

  identityLabel(value: BrokerSessionIdentityType): string {
    switch (value) {
      case 'bot':
        return 'Bot';
      case 'system':
        return 'System';
      case 'orphaned_bot_socket':
        return 'Orphaned bot socket';
      case 'ghost':
        return 'Ghost';
    }
  }

  identitySeverity(value: BrokerSessionIdentityType): TagSeverity {
    switch (value) {
      case 'bot':
        return 'success';
      case 'system':
        return 'info';
      case 'orphaned_bot_socket':
        return 'danger';
      case 'ghost':
        return 'warn';
    }
  }

  recencyLabel(value: BrokerSessionRecency): string {
    switch (value) {
      case 'current':
        return 'CURRENT';
      case 'past_closed':
        return 'PAST';
      case 'past_last_known':
        return 'PAST';
      case 'unknown':
        return 'UNKNOWN';
    }
  }

  recencySeverity(value: BrokerSessionRecency): TagSeverity {
    switch (value) {
      case 'current':
        return 'success';
      case 'past_closed':
      case 'past_last_known':
        return 'secondary';
      case 'unknown':
        return 'warn';
    }
  }

  recoveryLabel(value: BrokerSessionRecoveryState | null): string {
    switch (value) {
      case 'HEALTHY':
        return 'Healthy';
      case 'LINK_INTERRUPTED':
        return 'Link interrupted';
      case 'RESTORING':
        return 'Restoring';
      case 'SOCKET_DOWN':
        return 'Socket down';
      case 'RECONNECTING':
        return 'Reconnecting';
      case 'HARD_DOWN':
        return 'Hard down';
      case null:
        return 'Unknown';
    }
  }

  recoverySeverity(value: BrokerSessionRecoveryState | null): TagSeverity {
    switch (value) {
      case 'HEALTHY':
        return 'success';
      case 'HARD_DOWN':
      case 'SOCKET_DOWN':
        return 'danger';
      case 'LINK_INTERRUPTED':
      case 'RESTORING':
      case 'RECONNECTING':
        return 'warn';
      case null:
        return 'secondary';
    }
  }

  attentionLabel(code: BrokerSessionAttentionCode): string {
    switch (code) {
      case 'REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE':
        return 'Registry offline; socket live';
      case 'STARTED_BUT_NO_SOCKET':
        return 'Started; no socket';
      case 'SOCKET_WITHOUT_LIVE_PID':
        return 'No live PID';
      case 'ORPHANED_BOT_SOCKET':
        return 'Orphaned bot socket';
      case 'GHOST_SOCKET':
        return 'Unattributed socket';
      case 'GHOST_DETECTION_UNAVAILABLE':
        return 'Ghost detection unknown';
      case 'CLIENT_SIGNAL_STALE':
        return 'Client signal stale';
    }
  }

  attentionSeverity(code: BrokerSessionAttentionCode): TagSeverity {
    switch (code) {
      case 'ORPHANED_BOT_SOCKET':
      case 'SOCKET_WITHOUT_LIVE_PID':
        return 'danger';
      case 'REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE':
      case 'STARTED_BUT_NO_SOCKET':
      case 'GHOST_SOCKET':
      case 'GHOST_DETECTION_UNAVAILABLE':
      case 'CLIENT_SIGNAL_STALE':
        return 'warn';
    }
  }

  formatTimestamp(value: number | null): string {
    return fmtTimestampNy(value);
  }

  formatNumber(value: number | null): string {
    return fmtInteger(value);
  }

  eventsForRow(row: BrokerSessionRosterRow): readonly BrokerSessionEvent[] {
    if (row.client_id === null) return [];
    return this.events()
      .filter((event) => event.client_id === row.client_id)
      .slice(-10)
      .reverse();
  }

  readonly trackByRowId = (_index: number, row: BrokerSessionRosterRow): string =>
    row.row_id;

  private buildPurgeRequest(): BrokerSessionEventPurgeRequest | null {
    if (this.purgeConfirmText() !== BROKER_SESSION_PURGE_CONFIRM) return null;
    const clientId = parseOptionalNonNegativeInt(this.purgeClientIdText());
    const startMs = parseOptionalNonNegativeInt(this.purgeStartMsText());
    const endMs = parseOptionalNonNegativeInt(this.purgeEndMsText());
    if (clientId === undefined || startMs === undefined || endMs === undefined) {
      return null;
    }
    if (clientId === null && startMs === null && endMs === null) return null;
    if (startMs !== null && endMs !== null && startMs > endMs) return null;
    return {
      client_id: clientId,
      start_ms: startMs,
      end_ms: endMs,
      confirm: BROKER_SESSION_PURGE_CONFIRM,
    };
  }
}

function summarizeRows(rows: BrokerSessionRosterRow[]): MirrorSummary {
  return rows.reduce<MirrorSummary>(
    (summary, row) => {
      if (row.recency === 'current') summary.current += 1;
      else if (row.recency === 'unknown') summary.unknown += 1;
      else summary.past += 1;
      if (row.attention_codes.length > 0) summary.attention += 1;
      return summary;
    },
    { current: 0, past: 0, unknown: 0, attention: 0 },
  );
}

function humanError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return 'Could not load broker session mirror.';
}

function parseOptionalNonNegativeInt(value: string): number | null | undefined {
  const trimmed = value.trim();
  if (trimmed === '') return null;
  if (!/^\d+$/.test(trimmed)) return undefined;
  return Number(trimmed);
}
