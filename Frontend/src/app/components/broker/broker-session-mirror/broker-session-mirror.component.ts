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
  BrokerSessionRosterRow,
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
