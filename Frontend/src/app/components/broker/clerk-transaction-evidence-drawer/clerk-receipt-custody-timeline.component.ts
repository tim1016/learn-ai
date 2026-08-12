import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { ClerkCustodyTimeline } from '../../../api/clerk-transaction-history.types';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

type CustodyStageState = 'recorded' | 'awaiting' | 'not_recorded';

interface CustodyStage {
  readonly code: 'A0' | 'A1' | 'A2' | 'A3';
  readonly label: string;
  readonly explanation: string;
  readonly timestamp: number | null;
  readonly state: CustodyStageState;
}

/** Visual, fact-only rendering of the four durable Clerk custody checkpoints. */
@Component({
  selector: 'app-clerk-receipt-custody-timeline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './clerk-receipt-custody-timeline.component.html',
  styleUrl: './clerk-receipt-custody-timeline.component.scss',
})
export class ClerkReceiptCustodyTimelineComponent {
  readonly timeline = input<ClerkCustodyTimeline | null>(null);
  protected readonly stages = computed(() => buildCustodyStages(this.timeline()));
  protected readonly trackStage = (_: number, stage: CustodyStage): string => stage.code;
}

function buildCustodyStages(timeline: ClerkCustodyTimeline | null): readonly CustodyStage[] {
  const stages = [
    {
      code: 'A0' as const,
      label: 'Custody accepted',
      explanation: 'The Clerk durably accepted the instruction.',
      timestamp: timeline?.a0_custody_accepted_at_ms ?? null,
    },
    {
      code: 'A1' as const,
      label: 'Broker write started',
      explanation: 'The Clerk began the broker write.',
      timestamp: timeline?.broker_write_started_at_ms ?? null,
    },
    {
      code: 'A2' as const,
      label: 'Broker acknowledgement',
      explanation: 'A broker acknowledgement was recorded.',
      timestamp: timeline?.broker_ack_recorded_at_ms ?? null,
    },
    {
      code: 'A3' as const,
      label: 'Economic terminal',
      explanation: 'The economic outcome was durably recorded.',
      timestamp: timeline?.economic_terminal_recorded_at_ms ?? null,
    },
  ];
  const lastRecorded = stages.reduce(
    (latest, stage, index) => stage.timestamp === null ? latest : index,
    -1,
  );

  return stages.map((stage, index) => ({
    ...stage,
    state: stage.timestamp !== null
      ? 'recorded'
      : index === lastRecorded + 1
        ? 'awaiting'
        : 'not_recorded',
  }));
}
