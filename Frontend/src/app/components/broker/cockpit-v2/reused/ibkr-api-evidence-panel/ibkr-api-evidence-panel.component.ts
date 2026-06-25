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

import type { IbkrApiEvidenceEvent } from '../../../../../api/broker-models';
import { brokerSse, type SseStream } from '../../../../../services/broker-sse';
import { fmtTimestampNy } from '../../../format';

const MAX_EVENTS = 120;

interface EvidenceLine extends IbkrApiEvidenceEvent {
  display_ts: string;
  payload_json: string;
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
  private readonly stream = signal<SseStream<IbkrApiEvidenceEvent> | null>(null);

  readonly sseStatus = computed(() => this.stream()?.status() ?? 'idle');
  readonly sseError = computed(() => this.stream()?.lastError() ?? null);
  readonly lines = computed<EvidenceLine[]>(() => {
    const events = this.stream()?.data() ?? [];
    return events
      .slice(-MAX_EVENTS)
      .map((event) => ({
        ...event,
        display_ts: fmtTimestampNy(event.ts_ms),
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
    const stream = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrApiEvidenceEvent>('/api/broker/ibkr/evidence/stream?since_seq=0', 'ibkr_api', {
        maxBuffer: MAX_EVENTS,
      }),
    );
    this.stream.set(stream);
    this.destroyRef.onDestroy(() => stream.close());
  }

  trackLine = (_index: number, line: EvidenceLine): number => line.seq;
}
