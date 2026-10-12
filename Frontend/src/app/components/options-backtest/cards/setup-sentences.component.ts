import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { SetupSummary } from '../options-backtest.types';

/**
 * A setup in the server's sentences, exactly as served and in the order
 * served: notices first, then the window, each leg, the square off and the
 * costs. Every block is a list of whole sentences and each one is its own
 * line; the page never splits or rebuilds one.
 */
@Component({
  selector: 'app-options-backtest-setup-sentences',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './setup-sentences.component.html',
  styleUrl: './setup-sentences.component.scss',
})
export class SetupSentencesComponent {
  readonly summary = input.required<SetupSummary>();
}
