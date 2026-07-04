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
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from 'primeng/accordion';
import { ButtonModule } from 'primeng/button';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type {
  BrokerSessionAttentionItem,
  BrokerSessionDisplaySeverity,
  BrokerSessionEvent,
  BrokerSessionGlobalEvent,
  BrokerSessionHistoryPage,
  BrokerSessionHistoryPurgeRequest,
  BrokerSessionMirrorSnapshot,
  BrokerSessionMirrorSummary,
  BrokerSessionRosterRow,
} from '../../../api/broker-session-mirror.types';
import {
  BROKER_SESSION_PURGE_CONFIRM,
  type BrokerSessionEventPurgeRequest,
} from '../../../api/broker-session-mirror.types';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import { BrokerSessionMirrorService } from '../../../services/broker-session-mirror.service';
import { DaemonDiagnosticsStore } from '../../../services/daemon-diagnostics-store.service';
import { fmtInteger, fmtTimestampNy } from '../format';
import { operatorTagSeverity, type PrimeTagSeverity } from '../operator-severity';
import { DaemonDiagnosticsPanelComponent } from '../daemon-diagnostics/daemon-diagnostics-panel.component';
import { BrokerSessionEventsPanelComponent } from './broker-session-events-panel.component';

type AccordionValue = string | number | string[] | number[] | null | undefined;

const EMPTY_MIRROR_SUMMARY: BrokerSessionMirrorSummary = {
  current: 0,
  past: 0,
  unknown: 0,
  attention: 0,
};

type PurgeTarget = 'events' | 'history';

@Component({
  selector: 'app-broker-session-mirror',
  imports: [
    Accordion,
    AccordionContent,
    AccordionHeader,
    AccordionPanel,
    ButtonModule,
    BrokerSessionEventsPanelComponent,
    DaemonDiagnosticsPanelComponent,
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
  protected readonly daemonDiagnostics = inject(DaemonDiagnosticsStore);
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
  readonly historyAccordionValue = signal<string[]>([]);
  readonly isPurging = signal<boolean>(false);
  readonly purgeMessage = signal<string | null>(null);
  readonly purgeError = signal<string | null>(null);
  readonly purgeConfirmToken = BROKER_SESSION_PURGE_CONFIRM;

  readonly snapshot = computed<BrokerSessionMirrorSnapshot | null>(
    () => latestSnapshot(this.snapshotStream.latest(), this.manualSnapshot()),
  );
  readonly rows = computed<BrokerSessionRosterRow[]>(
    () => this.snapshot()?.rows ?? [],
  );
  readonly globalEvents = computed<BrokerSessionGlobalEvent[]>(
    () => this.snapshot()?.global_events ?? [],
  );
  readonly historySnapshots = computed<BrokerSessionMirrorSnapshot[]>(
    () => this.historyPage()?.rows ?? [],
  );
  readonly summary = computed<BrokerSessionMirrorSummary>(
    () => this.snapshot()?.summary ?? EMPTY_MIRROR_SUMMARY,
  );
  readonly streamStatus = this.snapshotStream.status;
  readonly streamError = this.snapshotStream.lastError;
  readonly eventStreamStatus = this.eventStream.status;
  readonly eventStreamError = this.eventStream.lastError;
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

  async refreshDaemonDiagnostics(): Promise<void> {
    await this.daemonDiagnostics.refresh();
  }

  async renewDaemonLeaseFromDiagnostics(): Promise<void> {
    await this.daemonDiagnostics.renewLease();
  }

  exportDaemonDiagnostics(): void {
    const report = this.daemonDiagnostics.report();
    if (report === null || typeof document === 'undefined') return;
    const blob = new Blob(
      [JSON.stringify({ note: 'Paths and sensitive fields were redacted before export.', report }, null, 2)],
      { type: 'application/json' },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `daemon-diagnostics-${report.fetched_at_ms}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  async navigateFromDaemonDiagnostics(path: string): Promise<void> {
    await this.router.navigateByUrl(path);
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
    return row.presentation.display_name;
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
    return row.presentation.broker.label;
  }

  primaryAttentionItem(row: BrokerSessionRosterRow): BrokerSessionAttentionItem | null {
    return row.attention_items[0] ?? null;
  }

  attentionTooltip(row: BrokerSessionRosterRow): string {
    if (row.attention_items.length === 0) return 'No attention codes';
    return row.attention_items
      .map((item) => item.summary ? `${item.label}: ${item.summary}` : item.label)
      .join('\n');
  }

  displaySeverity(severity: BrokerSessionDisplaySeverity): PrimeTagSeverity {
    return operatorTagSeverity(severity);
  }

  formatTimestamp(value: number | null): string {
    return fmtTimestampNy(value);
  }

  formatNumber(value: number | null): string {
    return fmtInteger(value);
  }

  historyPanelValue(snapshot: BrokerSessionMirrorSnapshot): string {
    return String(snapshot.as_of_ms);
  }

  historySnapshotOpen(snapshot: BrokerSessionMirrorSnapshot): boolean {
    return this.historyAccordionValue().includes(this.historyPanelValue(snapshot));
  }

  setHistoryAccordionValue(value: AccordionValue): void {
    this.historyAccordionValue.set(coerceAccordionValue(value));
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

  protected formatCount(value: number, singular: string): string {
    return `${this.formatNumber(value)} ${singular}${value === 1 ? '' : 's'}`;
  }
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

function coerceAccordionValue(value: AccordionValue): string[] {
  if (Array.isArray(value)) return value.map((item) => String(item));
  if (value === null || value === undefined) return [];
  return [String(value)];
}
