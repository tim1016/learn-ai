import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * The next-attempt facts every surface's projection carries (#2440): an
 * `EXIT_NOT_FLAT` on the account desk, the bot page's mission verdict and a
 * lane attention-bell item all have this shape.
 */
export interface NextAttemptFacts {
  readonly next_attempt_at_ms?: number | null;
  readonly exit_working?: boolean;
  readonly facts_unreadable?: boolean;
}

type NextAttemptLine =
  | { readonly kind: 'working' }
  | { readonly kind: 'at'; readonly atMs: number }
  | { readonly kind: 'unknown' };

/**
 * When an automatic recovery attempt is session-eligible —
 * the stuck-EXIT watchdog's re-drive of an `EXIT_NOT_FLAT` (#2440). One
 * rendering for every surface that shows it: the account desk, the bot page's
 * verdict and the lane's attention bell.
 *
 * The backend owns session eligibility. A timestamp is the
 * earliest retry opportunity, not a promised submission: quotes or custody
 * evidence may still prevent it. An active exit takes precedence over the
 * timestamp. Session eligibility renders in ET.
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
        Automatic retry: allowed from <app-timestamp-display
          [value]="shown.atMs"
          mode="et"
        />
      } @else {
        Automatic retry: eligibility unknown; this notice's record could not be read.
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
      return { kind: 'at', atMs: facts.next_attempt_at_ms };
    }
    return facts.facts_unreadable ? { kind: 'unknown' } : null;
  });
}
