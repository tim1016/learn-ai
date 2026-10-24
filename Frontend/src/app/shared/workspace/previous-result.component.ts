import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { ResultInputs } from './previous-result';

/**
 * Frames a shown result, and marks it a previous result once the inputs have
 * changed (PRD #3490 R04). Pair it with `createResultInputTracker`.
 *
 * While `previous` is true the result stays visible, dimmed, under a banner
 * that says it answers different inputs and lists them. Anything that would
 * consume the result (approve it, deploy it, use it as a starting point) goes
 * in `[resultActions]`, and is withdrawn while the result is previous, so a
 * stale answer cannot feed an approval by way of a button still on screen.
 *
 * Presentation only: it compares nothing. The host passes `previous` from
 * its tracker, and keeps gating its own logic on `answersCurrentInputs`.
 */
@Component({
  selector: 'app-previous-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './previous-result.component.html',
  styleUrl: './previous-result.component.scss',
})
export class PreviousResultComponent {
  readonly previous = input.required<boolean>();
  /** The inputs the shown result answers. */
  readonly captured = input.required<ResultInputs | null>();
}
