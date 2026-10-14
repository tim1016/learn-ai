import { ChangeDetectionStrategy, Component, input, model } from '@angular/core';

import type { SetupSummary } from '../options-backtest.types';

/**
 * A setup in the server's sentences, exactly as served and in the order
 * served: notices first, then the window, how orders fill (folded away, since
 * no setting changes it), each leg, the square off and the costs. Every block is a list of whole sentences and each one is its own
 * line; the page never splits or rebuilds one. A leg's block carries its
 * number in the setup, the one its card carries, and its id for the page to
 * find it by.
 */
@Component({
  selector: 'app-options-backtest-setup-sentences',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './setup-sentences.component.html',
  styleUrl: './setup-sentences.component.scss',
})
export class SetupSentencesComponent {
  readonly summary = input.required<SetupSummary>();
  /**
   * Whether how orders fill is open. A host that rebuilds this component
   * between readings binds it, so that the fold stays where the owner left it.
   */
  readonly fillsOpen = model(false);
}
