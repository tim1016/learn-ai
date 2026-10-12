import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import type { RunFigures } from '../options-backtest.types';

/**
 * The figures an owner judges a setup by. Every one is the server's: a dollar
 * figure is its exact string shown as given, a rate is the server's rounded
 * string beside the two whole numbers it came from, and nothing is worked
 * out here.
 */
@Component({
  selector: 'app-options-backtest-run-headline',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-headline.component.html',
  styleUrl: './run-headline.component.scss',
})
export class RunHeadlineComponent {
  readonly figures = input.required<RunFigures>();
}
