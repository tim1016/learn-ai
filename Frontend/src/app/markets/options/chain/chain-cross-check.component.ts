import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { ChainCrossCheck } from './chain-presentation';

/**
 * What a comparison column compares, printed before the table: the quantity and its units,
 * each side, the inputs both sides were valued with, the instants they are as of, and the
 * tolerance. The source adapter wrote every line; this prints them and adds no claim of its own.
 */
@Component({
  selector: 'app-chain-cross-check',
  templateUrl: './chain-cross-check.component.html',
  styleUrl: './chain-cross-check.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChainCrossCheckComponent {
  readonly crossCheck = input.required<ChainCrossCheck>();
}
