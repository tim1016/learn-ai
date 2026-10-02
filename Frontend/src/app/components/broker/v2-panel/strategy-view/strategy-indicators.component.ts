import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  output,
  viewChild,
} from '@angular/core';

import type { IndicatorCategory } from '../../../../shared/indicator-catalog/indicator-catalog.service';
import type { IndicatorPickerAdd } from '../../../../shared/indicator-picker/indicator-picker.component';
import { ChartIndicatorRailComponent } from '../../../../shared/trading-chart/chart-indicator-rail.component';
import type { TradingIndicatorChip } from '../../../../shared/trading-chart';
import type { StrategyViewValueSpec } from '../lib/broker-v2-panel.types';
import { anchorBelow, placePopover } from './popover-placement';

let nextIndicatorsId = 0;

/**
 * The Indicators chip (#2639 D12): the strategy's own drawn values first,
 * always on because they are the bot's recorded numbers, then the chart's
 * catalogue — searchable — whose lines the chart computes from the same
 * decision candles. The host owns which catalogue indicators are on.
 */
@Component({
  selector: 'app-strategy-indicators',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ChartIndicatorRailComponent],
  templateUrl: './strategy-indicators.component.html',
  styleUrl: './strategy-indicators.component.scss',
})
export class StrategyIndicatorsComponent {
  readonly values = input.required<readonly StrategyViewValueSpec[]>();
  readonly chartComputed = input<readonly TradingIndicatorChip[]>([]);
  readonly chartComputedKeys = input<readonly string[]>([]);
  readonly categories = input<readonly IndicatorCategory[]>([]);
  readonly catalogLoading = input(false);
  readonly catalogLoadFailed = input(false);
  readonly calculating = input(false);
  readonly error = input<string | null>(null);

  readonly indicatorAdded = output<IndicatorPickerAdd>();
  readonly indicatorRemoved = output<string>();

  protected readonly popoverId = `strategy-indicators-${nextIndicatorsId++}`;
  protected readonly titleId = `${this.popoverId}-title`;
  protected readonly drawn = computed(() => this.values().filter((value) => value.pane !== null));

  private readonly trigger = viewChild.required<ElementRef<HTMLButtonElement>>('trigger');
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  protected onToggle(event: Event): void {
    if ('newState' in event && event.newState === 'open') {
      placePopover(this.panel().nativeElement, anchorBelow(this.trigger().nativeElement));
    }
  }
}
