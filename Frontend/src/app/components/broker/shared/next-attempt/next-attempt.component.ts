import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { components } from '../../../../api/broker.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

export interface NextAttemptFacts {
  readonly recovery_status?: components['schemas']['RecoveryStatusResponse'] | null;
}

/** The Clerk's evaluated recovery status, shared by the desk, bot and lane bell. */
@Component({
  selector: 'app-next-attempt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  host: { '[style.display]': "facts().recovery_status ? null : 'none'" },
  styles: ':host { display: block; } .recovery-detail { display: block; }',
  template: `
    @if (facts().recovery_status; as status) {
      <span>{{ status.explanation }}</span>
      @if (status.kind === 'allowed_from' && status.allowed_from_ms !== null && status.allowed_from_ms !== undefined) {
        <span class="recovery-detail">Automatic retry: allowed from
          <app-timestamp-display [value]="status.allowed_from_ms" mode="et" />
        </span>
      }
      @if (status.kind === 'on_hold' || status.kind === 'broker_unreachable' || status.kind === 'stuck') {
        <span class="recovery-detail">{{ status.reason_code | receiptLabel }}</span>
      }
      @if (status.stuck_since_ms !== null && status.stuck_since_ms !== undefined) {
        <span class="recovery-detail">Position still open since
          <app-timestamp-display [value]="status.stuck_since_ms" mode="et" />
        </span>
      }
      @if (status.last_checked_at_ms !== null && status.last_checked_at_ms !== undefined) {
        <span class="recovery-detail">Last checked
          <app-timestamp-display [value]="status.last_checked_at_ms" />
        </span>
      }
    }
  `,
})
export class NextAttemptComponent {
  readonly facts = input.required<NextAttemptFacts>();
}
