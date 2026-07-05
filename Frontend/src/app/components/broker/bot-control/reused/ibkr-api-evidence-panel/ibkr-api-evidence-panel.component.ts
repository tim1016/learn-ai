import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  Injector,
  computed,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type {
  DataPlaneHealth,
  DiagnosticReport,
  IbkrApiEvidenceEvent,
  IbkrConnectionHealth,
  IbkrSerializerWarning,
} from '../../../../../api/broker-models';
import { brokerSse, type SseStream } from '../../../../../services/broker-sse';
import { BrokerService } from '../../../../../services/broker.service';
import { AssetIdentityComponent } from '../../../../../shared/asset-identity';
import { fmtTimestampNy } from '../../../format';

const MAX_EVENTS = 120;

interface EvidenceLine extends IbkrApiEvidenceEvent {
  display_ts: string;
  payload_json: string;
  serializer_warnings: IbkrSerializerWarning[];
}

@Component({
  selector: 'app-ibkr-api-evidence-panel',
  imports: [AssetIdentityComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './ibkr-api-evidence-panel.component.html',
  styleUrl: './ibkr-api-evidence-panel.component.scss',
})
export class IbkrApiEvidencePanelComponent {
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);
  private readonly broker = inject(BrokerService);
  private readonly stream = signal<SseStream<IbkrApiEvidenceEvent> | null>(null);
  private readonly backfillEvents = signal<IbkrApiEvidenceEvent[]>([]);

  readonly loading = signal(true);
  readonly snapshotError = signal<string | null>(null);
  readonly dataPlane = signal<DataPlaneHealth | null>(null);
  readonly connectionHealth = signal<IbkrConnectionHealth | null>(null);
  readonly diagnostic = signal<DiagnosticReport | null>(null);
  readonly fmtTimestampNy = fmtTimestampNy;
  readonly sseStatus = computed(() => this.stream()?.status() ?? 'idle');
  readonly sseError = computed(() => this.stream()?.lastError() ?? null);
  readonly diagnosticChecks = computed(() => {
    const diagnostic = this.diagnostic();
    return diagnostic && !diagnostic.disabled ? diagnostic.checks : [];
  });
  readonly lines = computed<EvidenceLine[]>(() => {
    // HTTP backfill keeps diagnostics visible if SSE is unavailable; SSE carries live updates.
    const events = [...this.backfillEvents(), ...(this.stream()?.data() ?? [])];
    return dedupeBySeq(events)
      .slice(-MAX_EVENTS)
      .map((event) => ({
        ...event,
        display_ts: fmtTimestampNy(event.ts_ms),
        serializer_warnings: event.response?.serializer_warnings ?? [],
        payload_json: JSON.stringify(
          {
            request: event.request,
            response: event.response,
            error: event.error,
          },
          null,
          2,
        ),
      }))
      .reverse();
  });

  constructor() {
    void this.loadSnapshot();
  }

  trackLine = (_index: number, line: EvidenceLine): number => line.seq;

  private async loadSnapshot(): Promise<void> {
    this.loading.set(true);
    this.snapshotError.set(null);
    try {
      const [dataPlane, health, diagnostic, evidence] = await Promise.allSettled([
        this.broker.dataPlaneHealth(),
        this.broker.health(),
        this.broker.diagnose(),
        this.broker.ibkrApiEvidence(0, MAX_EVENTS),
      ]);
      if (this.destroyRef.destroyed) return;
      if (dataPlane.status === 'fulfilled') this.dataPlane.set(dataPlane.value);
      if (health.status === 'fulfilled') this.connectionHealth.set(health.value);
      if (diagnostic.status === 'fulfilled') this.diagnostic.set(diagnostic.value);
      const events = evidence.status === 'fulfilled' ? evidence.value : [];
      this.backfillEvents.set(events);
      const failed = [dataPlane, health, diagnostic, evidence].filter(
        (result) => result.status === 'rejected',
      );
      if (failed.length) {
        this.snapshotError.set('Some broker diagnostics failed to load.');
      }
      this.openStream(maxSeq(events));
    } catch (error) {
      if (this.destroyRef.destroyed) return;
      this.snapshotError.set((error as Error).message || 'Could not load broker diagnostics.');
      this.openStream(0);
    } finally {
      if (!this.destroyRef.destroyed) this.loading.set(false);
    }
  }

  private openStream(sinceSeq: number): void {
    if (this.destroyRef.destroyed) return;
    const stream = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrApiEvidenceEvent>(
        `/api/broker/ibkr/evidence/stream?since_seq=${sinceSeq}`,
        'ibkr_api',
        { maxBuffer: MAX_EVENTS },
      ),
    );
    this.stream.set(stream);
    this.destroyRef.onDestroy(() => stream.close());
  }
}

function maxSeq(events: readonly IbkrApiEvidenceEvent[]): number {
  return events.reduce((max, event) => Math.max(max, event.seq), 0);
}

function dedupeBySeq(events: readonly IbkrApiEvidenceEvent[]): IbkrApiEvidenceEvent[] {
  const bySeq = new Map<number, IbkrApiEvidenceEvent>();
  for (const event of events) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}
