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
import { fmtTimestampNy } from '../../../format';

const MAX_EVENTS = 120;

interface EvidenceLine extends IbkrApiEvidenceEvent {
  display_ts: string;
  payload_json: string;
  serializer_warnings: IbkrSerializerWarning[];
}

@Component({
  selector: 'app-ibkr-api-evidence-panel',
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
      const [dataPlane, health, diagnostic, evidence] = await Promise.all([
        this.broker.dataPlaneHealth(),
        this.broker.health(),
        this.broker.diagnose(),
        this.broker.ibkrApiEvidence(0, MAX_EVENTS),
      ]);
      this.dataPlane.set(dataPlane);
      this.connectionHealth.set(health);
      this.diagnostic.set(diagnostic);
      this.backfillEvents.set(evidence);
      this.openStream(maxSeq(evidence));
    } catch (error) {
      this.snapshotError.set((error as Error).message || 'Could not load broker diagnostics.');
      this.openStream(0);
    } finally {
      this.loading.set(false);
    }
  }

  private openStream(sinceSeq: number): void {
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
