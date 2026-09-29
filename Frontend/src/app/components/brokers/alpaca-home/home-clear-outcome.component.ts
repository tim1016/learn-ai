import { ChangeDetectionStrategy, Component, computed, type ElementRef, input, output, viewChild } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { HOME_CLEAR_COPY, clearRetryCanChange, isCleared, type ClearOutcome } from './home-clear';

/**
 * What the last clear did, per bot, in request order: cleared, or not cleared
 * in the backend's own words — its headline and reason as written, its code
 * through the shared label pipe. A request that returned no per-bot result
 * says so, with a retry of the same batch when one could change anything.
 */
@Component({
  selector: 'app-home-clear-outcome',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  templateUrl: './home-clear-outcome.component.html',
  styleUrl: './home-clear-outcome.component.scss',
})
export class HomeClearOutcomeComponent {
  readonly outcome = input.required<ClearOutcome>();
  readonly busy = input(false);

  readonly retryRequested = output();
  readonly dismissed = output();

  protected readonly copy = HOME_CLEAR_COPY;
  private readonly summaryLine = viewChild.required<ElementRef<HTMLElement>>('summaryLine');

  protected readonly summary = computed(() => {
    const outcome = this.outcome();
    switch (outcome.kind) {
      case 'result':
        return HOME_CLEAR_COPY.summary(outcome.result.legs.filter(isCleared).length, outcome.requested.length);
      case 'unknown':
        return HOME_CLEAR_COPY.requestUnknown;
      case 'refused':
        return outcome.message;
    }
  });

  /** Anything not cleared is said as an alert; a clean clear as a status. */
  protected readonly unresolved = computed(() => {
    const outcome = this.outcome();
    return outcome.kind !== 'result' || outcome.result.legs.some((leg) => !isCleared(leg))
      || outcome.result.legs.length < outcome.requested.length;
  });

  protected readonly notAttempted = computed(() => {
    const outcome = this.outcome();
    if (outcome.kind !== 'result') return [];
    const answered = new Set(outcome.result.legs.map((leg) => leg.strategy_instance_id));
    return outcome.requested.filter((sid) => !answered.has(sid));
  });

  protected readonly canRetry = computed(() => clearRetryCanChange(this.outcome()));

  /** Move the keyboard to the summary once a clear has answered. */
  focus(): void {
    this.summaryLine().nativeElement.focus();
  }
}
