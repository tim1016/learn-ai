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
  BrokerSessionHistoryPage,
  BrokerSessionHistoryPurgeRequest,
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

type PurgeTarget = 'events' | 'history';

@Component({
  selector: 'app-broker-session-mirror',
  imports: [
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
        { maxBuffer: 1, dataPlaneControlIntent: true },
      ),
    );
  private readonly eventStream: SseStream<BrokerSessionEvent> =
    runInInjectionContext(this.injector, () =>
      brokerSse<BrokerSessionEvent>(
        '/api/broker/session-mirror/events/stream',
        'broker_event',
        { maxBuffer: 500, dataPlaneControlIntent: true },
      ),
    );

  readonly manualSnapshot = signal<BrokerSessionMirrorSnapshot | null>(null);
  readonly historyPage = signal<BrokerSessionHistoryPage | null>(null);
  readonly isRefreshing = signal<boolean>(false);
  readonly isRefreshingHistory = signal<boolean>(false);
  readonly refreshError = signal<string | null>(null);
  readonly historyError = signal<string | null>(null);
  readonly purgeClientIdText = signal<string>('');
  readonly purgeStartMsText = signal<string>('');
  readonly purgeEndMsText = signal<string>('');
  readonly purgeConfirmText = signal<string>('');
  readonly purgeTarget = signal<PurgeTarget>('events');
  readonly expandedHistorySnapshots = signal<ReadonlySet<number>>(new Set());
  readonly isPurging = signal<boolean>(false);
  readonly purgeMessage = signal<string | null>(null);
  readonly purgeError = signal<string | null>(null);
  readonly purgeConfirmToken = BROKER_SESSION_PURGE_CONFIRM;
  private readonly purgedEventFilters = signal<
    readonly BrokerSessionEventPurgeRequest[]
  >([]);

  readonly snapshot = computed<BrokerSessionMirrorSnapshot | null>(
    () => latestSnapshot(this.snapshotStream.latest(), this.manualSnapshot()),
  );
  readonly rows = computed<BrokerSessionRosterRow[]>(
    () => this.snapshot()?.rows ?? [],
  );
  readonly historySnapshots = computed<BrokerSessionMirrorSnapshot[]>(
    () => this.historyPage()?.rows ?? [],
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
    void this.refreshHistory();
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

  async refreshHistory(): Promise<void> {
    this.isRefreshingHistory.set(true);
    this.historyError.set(null);
    try {
      this.historyPage.set(await this.mirror.history({ limit: 12 }));
    } catch (err) {
      this.historyError.set(humanError(err));
    } finally {
      this.isRefreshingHistory.set(false);
    }
  }

  async openBot(row: BrokerSessionRosterRow): Promise<void> {
    if (!row.strategy_instance_id) return;
    await this.router.navigate(['/broker/bots', row.strategy_instance_id]);
  }

  selectPurgeTarget(target: PurgeTarget): void {
    this.purgeTarget.set(target);
    this.purgeMessage.set(null);
    this.purgeError.set(null);
  }

  async purgeDiagnostics(): Promise<void> {
    const request = this.buildPurgeRequest();
    if (request === null) return;
    this.isPurging.set(true);
    this.purgeError.set(null);
    this.purgeMessage.set(null);
    try {
      if (this.purgeTarget() === 'events') {
        const result = await this.mirror.purgeEvents(request);
        this.purgeConfirmText.set('');
        this.purgedEventFilters.update((prev) => [...prev, request]);
        this.purgeMessage.set(
          `Purged ${this.formatCount(result.purged_count, 'event')}; ${this.formatNumber(result.remaining_count)} remain.`,
        );
        await this.refresh();
      } else {
        const result = await this.mirror.purgeHistory(request);
        this.purgeConfirmText.set('');
        this.purgeMessage.set(
          `Purged ${this.formatCount(result.purged_row_count, 'history row')}; ${this.formatCount(result.purged_snapshot_count, 'snapshot')} removed; ${this.formatCount(result.remaining_snapshot_count, 'snapshot')} remain.`,
        );
        await this.refreshHistory();
      }
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

  rowDisplayName(row: BrokerSessionRosterRow): string {
    return row.strategy_instance_id ?? row.command ?? 'Unattributed session';
  }

  clientTooltip(row: BrokerSessionRosterRow): string {
    return [
      this.rowDisplayName(row),
      `row_id: ${row.row_id}`,
      `run_id: ${row.run_id ?? '-'}`,
      `account: ${row.account_id ?? '-'}`,
      `run_dir: ${row.run_dir ?? '-'}`,
    ].join('\n');
  }

  socketLabel(row: BrokerSessionRosterRow): string {
    if (!row.socket_present) return 'Missing';
    return `${row.local_port ?? '-'} -> ${row.remote_port ?? '-'}`;
  }

  brokerLabel(row: BrokerSessionRosterRow): string {
    return row.connection_state ?? '-';
  }

  primaryAttentionCode(row: BrokerSessionRosterRow): BrokerSessionAttentionCode | null {
    return row.attention_codes[0] ?? null;
  }

  attentionTooltip(row: BrokerSessionRosterRow): string {
    if (row.attention_codes.length === 0) return 'No attention codes';
    return row.attention_codes.map((code) => this.attentionLabel(code)).join('\n');
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
    if (!rowCanAttachClientEvents(row)) return [];
    return this.events()
      .filter((event) => eventMatchesRow(event, row))
      .filter((event) => !eventWasPurged(event, this.purgedEventFilters()))
      .slice(-10)
      .reverse();
  }

  historyRows(snapshot: BrokerSessionMirrorSnapshot): readonly BrokerSessionRosterRow[] {
    return this.historySnapshotExpanded(snapshot) ? snapshot.rows : snapshot.rows.slice(0, 4);
  }

  historyOverflowCount(snapshot: BrokerSessionMirrorSnapshot): number {
    return Math.max(0, snapshot.rows.length - 4);
  }

  historySnapshotExpanded(snapshot: BrokerSessionMirrorSnapshot): boolean {
    return this.expandedHistorySnapshots().has(snapshot.as_of_ms);
  }

  toggleHistorySnapshot(snapshot: BrokerSessionMirrorSnapshot): void {
    const next = new Set(this.expandedHistorySnapshots());
    if (next.has(snapshot.as_of_ms)) next.delete(snapshot.as_of_ms);
    else next.add(snapshot.as_of_ms);
    this.expandedHistorySnapshots.set(next);
  }

  purgeTargetSeverity(target: PurgeTarget): 'secondary' | undefined {
    return this.purgeTarget() === target ? undefined : 'secondary';
  }

  purgeButtonLabel(): string {
    return this.purgeTarget() === 'events' ? 'Purge events' : 'Purge history';
  }

  readonly trackByRowId = (_index: number, row: BrokerSessionRosterRow): string =>
    row.row_id;

  private buildPurgeRequest():
    | BrokerSessionEventPurgeRequest
    | BrokerSessionHistoryPurgeRequest
    | null {
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

  private formatCount(value: number, singular: string): string {
    return `${this.formatNumber(value)} ${singular}${value === 1 ? '' : 's'}`;
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

function latestSnapshot(
  streamSnapshot: BrokerSessionMirrorSnapshot | null,
  manualSnapshot: BrokerSessionMirrorSnapshot | null,
): BrokerSessionMirrorSnapshot | null {
  if (streamSnapshot === null) return manualSnapshot;
  if (manualSnapshot === null) return streamSnapshot;
  return manualSnapshot.as_of_ms > streamSnapshot.as_of_ms
    ? manualSnapshot
    : streamSnapshot;
}

function rowCanAttachClientEvents(row: BrokerSessionRosterRow): boolean {
  return (
    row.client_id !== null &&
    (row.identity_type === 'system' ||
      (row.registry_claim !== null && row.registry_claim.started_at_ms !== null))
  );
}

function eventMatchesRow(
  event: BrokerSessionEvent,
  row: BrokerSessionRosterRow,
): boolean {
  if (event.client_id !== row.client_id || event.ts_ms > row.as_of_ms) return false;
  const startedAtMs = row.registry_claim?.started_at_ms ?? null;
  return startedAtMs === null || event.ts_ms >= startedAtMs;
}

function eventWasPurged(
  event: BrokerSessionEvent,
  filters: readonly BrokerSessionEventPurgeRequest[],
): boolean {
  return filters.some((filter) => {
    if (filter.client_id !== null && filter.client_id !== undefined) {
      if (event.client_id !== filter.client_id) return false;
    }
    if (filter.start_ms !== null && filter.start_ms !== undefined) {
      if (event.ts_ms < filter.start_ms) return false;
    }
    if (filter.end_ms !== null && filter.end_ms !== undefined) {
      if (event.ts_ms > filter.end_ms) return false;
    }
    return true;
  });
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
