import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe, formatAuthoredUsd } from '../../../shared/pipes/authored-usd.pipe';
import type { SlippagePoint, SlippageTolerance } from '../options-backtest.types';
import { toleranceSentence } from '../run-words';
import { isLoss, scaledBarPercents } from './run-chart-scale';

/**
 * How much slippage the result survives: one sentence, and the ladder it was
 * read off, each row the whole run replayed at one slippage, the setup's own
 * row marked.
 */
@Component({
  selector: 'app-options-backtest-slippage-tolerance',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './slippage-tolerance.component.html',
  styleUrl: './slippage-tolerance.component.scss',
})
export class SlippageToleranceComponent {
  readonly tolerance = input.required<SlippageTolerance>();
  readonly points = input.required<readonly SlippagePoint[]>();

  protected readonly sentence = computed(() => toleranceSentence(this.tolerance()));
  protected readonly bars = computed(() => {
    const points = this.points();
    const widths = scaledBarPercents(points.map((point) => point.net_profit_usd), 50);
    return points.map((point, index) => ({
      ...point,
      loss: isLoss(point.net_profit_usd),
      widthCss: `${widths[index]}%`,
      label: `${point.slippage_percent}% slippage: ${formatAuthoredUsd(point.net_profit_usd)} net profit`,
    }));
  });
}
