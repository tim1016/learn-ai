import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * The next-attempt facts every surface's projection carries (#2440): an
 * `EXIT_NOT_FLAT` on the account desk, the bot page's mission verdict and a
 * lane attention-bell item all have this shape.
 */
export interface NextAttemptFacts {
  readonly next_attempt_at_ms?: number | null;
  readonly next_attempt_overdue?: boolean;
  readonly exit_working?: boolean;
  readonly facts_unreadable?: boolean;
}

type NextAttemptLine =
  | { readonly kind: 'working' }
  | { readonly kind: 'at'; readonly atMs: number; readonly overdue: boolean }
  | { readonly kind: 'unknown' };

/**
 * When the Clerk next tries, on its own, to resolve a condition it raised —
 * the stuck-EXIT watchdog's re-drive of an `EXIT_NOT_FLAT` (#2440). One
 * rendering for every surface that shows it: the account desk, the bot page's
 * verdict and the lane's attention bell.
 *
 * The backend projects it on every read: while an exit is in progress no
 * attempt is due, so that is said instead of a time — never "overdue", which
 * would read as a failed automatic sell and invite a second, manual one. A
 * time is the watchdog's real next try; "overdue since" only when it could
 * have sent by now. A record that could not be read is an unknown attempt,
 * not an absent one. Rendered in ET: a session-clock instant, like the
 * pre-market open it usually names. Nothing renders when there is nothing to
 * say.
 */
@Component({
  selector: 'app-next-attempt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  host: { '[style.display]': "line() ? null : 'none'" },
  styles: ':host { display: block; }',
  template: `
    @if (line(); as shown) {
      @if (shown.kind === 'working') {
        An exit is in progress; no automatic attempt is due while it works.
      } @else if (shown.kind === 'at') {
        Next automatic attempt: @if (shown.overdue) {overdue since }<app-timestamp-display
          [value]="shown.atMs"
          mode="et"
        />
      } @else {
        Next automatic attempt: unknown; this notice's record could not be read.
      }
    }
  `,
})
export class NextAttemptComponent {
  readonly facts = input.required<NextAttemptFacts>();

  protected readonly line = computed((): NextAttemptLine | null => {
    const facts = this.facts();
    if (facts.exit_working) {
      return { kind: 'working' };
    }
    if (facts.next_attempt_at_ms != null) {
      return { kind: 'at', atMs: facts.next_attempt_at_ms, overdue: facts.next_attempt_overdue ?? false };
    }
    return facts.facts_unreadable ? { kind: 'unknown' } : null;
  });
}
