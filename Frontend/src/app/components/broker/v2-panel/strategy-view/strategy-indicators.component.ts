import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  viewChild,
} from '@angular/core';

import type { StrategyViewValueSpec } from '../lib/broker-v2-panel.types';
import { anchorBelow, placePopover } from './popover-placement';

let nextIndicatorsId = 0;

/**
 * The Indicators chip (#2639): the strategy's own drawn values, always on,
 * because they are the bot's recorded numbers rather than the chart's.
 * Chart-computed indicators from the catalogue join this list later.
 */
@Component({
  selector: 'app-strategy-indicators',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './strategy-indicators.component.html',
  styleUrl: './strategy-indicators.component.scss',
})
export class StrategyIndicatorsComponent {
  readonly values = input.required<readonly StrategyViewValueSpec[]>();

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
