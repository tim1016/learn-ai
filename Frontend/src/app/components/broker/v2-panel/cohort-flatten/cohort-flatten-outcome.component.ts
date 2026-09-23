import {
  ChangeDetectionStrategy,
  Component,
  computed,
  type ElementRef,
  input,
  viewChild,
} from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { CohortActionResult, CohortLegResult } from '../lib/broker-v2-panel.types';

/** Closed operator-copy map for the five typed leg outcomes. */
const OUTCOME_LABEL: Readonly<Record<CohortLegResult['outcome'], string>> = {
  applied: 'Applied',
  replayed: 'Replayed (already applied)',
  refused: 'Refused',
  failed: 'Failed',
  unknown: 'Outcome unknown',
};

/**
 * What one flatten wave actually did, per leg, in request order.
 *
 * Applied and replayed legs show their receipt; refused, failed and unknown
 * legs show the typed refusal — `reason_code` through the shared label pipe,
 * `message`/`why` as the backend wrote them, receipt ids verbatim.
 *
 * The batch ends early only on account-scoped authority loss (ADR 0051): the
 * response then carries fewer legs than were sent, the last of which holds the
 * account's refusal. That case is rendered as the account-scoped blocker with
 * the legs it never reached named, while the attempted legs keep their
 * outcomes below it — never collapsed into one failure for the whole wave.
 */
@Component({
  selector: 'app-cohort-flatten-outcome',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './cohort-flatten-outcome.component.html',
  styleUrl: './cohort-flatten-outcome.component.scss',
})
export class CohortFlattenOutcomeComponent {
  readonly result = input.required<CohortActionResult>();
  /** The sids the wave sent, in request order. */
  readonly requested = input.required<readonly string[]>();

  private readonly region = viewChild.required<ElementRef<HTMLElement>>('region');

  protected readonly outcomeLabel = OUTCOME_LABEL;

  protected readonly unresolved = computed(
    () => this.result().refused_count + this.result().failed_count > 0,
  );

  protected readonly notAttempted = computed(() => {
    const answered = new Set(this.result().legs.map((leg) => leg.strategy_instance_id));
    return this.requested().filter((sid) => !answered.has(sid));
  });

  /** The account's refusal that ended the batch, when it ended early. */
  protected readonly accountBlocker = computed(() => {
    if (this.notAttempted().length === 0) return null;
    return this.result().legs.at(-1)?.error ?? null;
  });

  /** Move keyboard focus to the outcome once a wave has answered. */
  focus(): void {
    this.region().nativeElement.focus();
  }
}
