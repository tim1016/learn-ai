import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, contentChild, input, type TemplateRef } from '@angular/core';

import type { ResultInputs } from './previous-result';

/**
 * Frames a shown result, and marks it a previous result once the inputs have
 * changed (PRD #3490 R04). Pair it with `createResultInputTracker`.
 *
 * While `previous` is true the result stays visible, dimmed, under a banner
 * that says it answers different inputs and lists them. Anything that would
 * consume the result (approve it, deploy it, use it as a starting point) goes
 * in the actions area, which is not drawn while the result is previous, so a
 * stale answer cannot feed an approval by way of a button still on screen.
 *
 * Two ways to fill it:
 *   - `[resultActions]` content: hidden while previous, but still alive. The
 *     host builds it, so a component in it keeps its state and effects. Fit
 *     for plain buttons.
 *   - `<ng-template #resultActions>`: created only while the result is
 *     current and destroyed when it turns previous. Use it for a component
 *     that must not run against a stale answer.
 *
 * Presentation only: it compares nothing. The host passes `previous` from
 * its tracker, and keeps gating its own logic on `answersCurrentInputs`.
 */
@Component({
  selector: 'app-previous-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgTemplateOutlet],
  templateUrl: './previous-result.component.html',
  styleUrl: './previous-result.component.scss',
})
export class PreviousResultComponent {
  readonly previous = input.required<boolean>();
  /** The inputs the shown result answers. */
  readonly captured = input.required<ResultInputs | null>();

  /** Actions given as a template, which exist only while the result is current. */
  protected readonly actionsTemplate = contentChild<TemplateRef<unknown>>('resultActions');
}
