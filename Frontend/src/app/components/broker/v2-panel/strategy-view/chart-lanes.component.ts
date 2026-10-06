import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';
import type { PlacedLane } from './chart-lanes';

/**
 * The run's lanes under a chart, drawn where that chart placed them
 * (`placeLanes`): the strategy chart's decision candles (#2794 R4) and the
 * tape's bars (#2808) share it. Each lane's title sits in the price scale's
 * gutter, so a mark at the left edge never hides it.
 */
@Component({
  selector: 'app-chart-lanes',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  templateUrl: './chart-lanes.component.html',
  styleUrl: './chart-lanes.component.scss',
})
export class ChartLanesComponent {
  readonly lanes = input.required<readonly PlacedLane[]>();
}
