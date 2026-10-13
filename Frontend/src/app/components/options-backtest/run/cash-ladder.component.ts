import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import type { RunFigures } from '../options-backtest.types';
import { capitalEventLabel } from '../run-words';

/**
 * The same run replayed at several starting cashes, each row the whole run at
 * one cash with the setup's own marked (#3108). It shows how participation
 * changes with cash: what a smaller cash refuses and a larger one fills. No row
 * is called safe, and a run saved before the ladder was kept says "not recorded".
 */
@Component({
  selector: 'app-options-backtest-cash-ladder',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './cash-ladder.component.html',
  styleUrl: './cash-ladder.component.scss',
})
export class CashLadderComponent {
  readonly figures = input.required<RunFigures>();

  protected readonly levels = computed(() => this.figures().cash_ladder ?? null);
  protected readonly eventLabel = capitalEventLabel;
}
