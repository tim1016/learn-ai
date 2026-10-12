import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import type { RunBanner } from '../options-backtest.types';

/**
 * What the owner must not mistake the figures for: fills are simulated from
 * trade prices, bid and ask are not available, and a slippage and a fee
 * policy were assumed. It sits above the figures and is never collapsed.
 */
@Component({
  selector: 'app-options-backtest-run-banner',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-banner.component.html',
  styleUrl: './run-banner.component.scss',
})
export class RunBannerComponent {
  readonly banner = input.required<RunBanner>();
}
