import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { ClerkCustodyTimeline } from '../../../api/clerk-transaction-history.types';
import { TimestampDisplayComponent } from '../../../shared/timestamp';

interface CustodyStage {
  readonly code: 'A0' | 'A2' | 'A3';
  readonly label: string;
  readonly explanation: string;
  readonly timestamp: number | null;
}

/** Visual, fact-only rendering of the recorded Clerk custody checkpoints. */
@Component({
  selector: 'app-clerk-receipt-custody-timeline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './clerk-receipt-custody-timeline.component.html',
  styleUrl: './clerk-receipt-custody-timeline.component.scss',
})
export class ClerkReceiptCustodyTimelineComponent {
  readonly timeline = input<ClerkCustodyTimeline | null>(null);
  /** The order was placed outside the Clerk: it observed it, and accepted no instruction. */
  readonly external = input(false);
  protected readonly stages = computed(() => buildCustodyStages(this.timeline(), this.external()));
  protected readonly trackStage = (_: number, stage: CustodyStage): string => stage.code;
}

function buildCustodyStages(timeline: ClerkCustodyTimeline | null, external: boolean): readonly CustodyStage[] {
  const stages = [
    {
      code: 'A0' as const,
      label: external ? 'Order observed' : 'Custody accepted',
      explanation: external
        ? 'The Clerk first recorded this order. It was placed outside the Clerk, which accepted no instruction for it.'
        : 'The Clerk durably accepted the instruction.',
      timestamp: timeline?.a0_custody_accepted_at_ms ?? null,
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
  // A marker with no timestamp is not recorded. Which one comes next is not inferred: no producer
  // promises the stages in order, and an order whose outcome is unknown has none after A0.
  return stages;
}
