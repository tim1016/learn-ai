import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { Drawdown, RunFigures } from '../options-backtest.types';

interface DrawdownEntry {
  readonly title: string;
  readonly drawdown: Drawdown | null;
}

/**
 * The largest fall of equity from a peak, twice: the worst in dollars and the
 * worst as a fraction of the peak, each with its peak and its trough. Both are
 * measured at the end of each session, and say so.
 */
@Component({
  selector: 'app-options-backtest-run-drawdown',
  imports: [AuthoredUsdPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-drawdown.component.html',
  styleUrl: './run-drawdown.component.scss',
})
export class RunDrawdownComponent {
  readonly figures = input.required<RunFigures>();

  protected readonly entries = computed<readonly DrawdownEntry[]>(() => [
    { title: 'Largest fall in dollars', drawdown: this.figures().drawdown_by_dollars ?? null },
    { title: 'Largest fall as a fraction of the peak', drawdown: this.figures().drawdown_by_fraction ?? null },
  ]);
}
