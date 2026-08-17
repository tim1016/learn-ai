import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { IndicatorCategory } from '../indicator-catalog/indicator-catalog.service';
import {
  IndicatorPickerComponent,
  type IndicatorPickerAdd,
} from '../indicator-picker/indicator-picker.component';
import type { TradingIndicatorChip } from './trading-chart.types';

/** Shared expanded-chart rail for selecting and removing indicator series. */
@Component({
  selector: 'app-chart-indicator-rail',
  imports: [IndicatorPickerComponent],
  templateUrl: './chart-indicator-rail.component.html',
  styleUrl: './chart-indicator-rail.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    role: 'complementary',
    'aria-label': 'Indicator picker rail',
  },
})
export class ChartIndicatorRailComponent {
  readonly activeIndicators = input<readonly TradingIndicatorChip[]>([]);
  readonly activeKeys = input<readonly string[]>([]);
  readonly categories = input<readonly IndicatorCategory[]>([]);
  readonly catalogLoading = input(false);
  readonly calculationLoading = input(false);
  readonly error = input<string | null>(null);

  readonly indicatorAdded = output<IndicatorPickerAdd>();
  readonly indicatorRemoved = output<string>();
}
