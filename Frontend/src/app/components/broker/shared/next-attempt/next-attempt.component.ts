import { ChangeDetectionStrategy, Component, booleanAttribute, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * When the Clerk next tries, on its own, to resolve a condition it raised —
 * the stuck-EXIT watchdog's re-drive of an `EXIT_NOT_FLAT` (#2440). One
 * rendering for every surface that shows it: the account desk, the bot page's
 * verdict and the lane's attention bell.
 *
 * The backend sends the instant (`int64 ms UTC`) and whether it has already
 * passed; a deferred try records nothing, so a past time is said to be
 * overdue, never shown as a future promise. Rendered in ET: it is a
 * session-clock instant, like the pre-market open it usually names.
 */
@Component({
  selector: 'app-next-attempt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  template: `Next automatic attempt: @if (overdue()) {overdue since }<app-timestamp-display [value]="atMs()" mode="et" />`,
})
export class NextAttemptComponent {
  readonly atMs = input.required<number>();
  /** The backend's `next_attempt_overdue`; absent reads as not overdue. */
  readonly overdue = input(false, { transform: booleanAttribute });
}
