import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
  output,
  signal,
} from '@angular/core';

import type { StrategyViewGateView, StrategyViewResponse } from '../lib/broker-v2-panel.types';
import type { PopoverAnchor } from './popover-placement';
import { StrategyCandlePopoverComponent } from './strategy-candle-popover.component';
import { StrategyChartComponent, type StrategyCandleClick } from './strategy-chart.component';
import {
  GATE_CANDLE_COLORS,
  candleAt,
  decisionTimeframeLabel,
  lineColorVar,
  strategyLinePlans,
  type StrategyViewFailure,
} from './strategy-view-model';

/**
 * The strategy tab's body (#2639): the backend's notices, a legend, the bot's
 * decision candles shaded by the active gate, and the clicked candle's
 * popover — or a calm empty state, or the backend's reason the read failed.
 *
 * Selection is two-way (`selectedBarCloseMs`) so the decisions list beside
 * the chart can share it; the popover is this view's own.
 */
@Component({
  selector: 'app-strategy-view',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [StrategyCandlePopoverComponent, StrategyChartComponent],
  templateUrl: './strategy-view.component.html',
  styleUrl: './strategy-view.component.scss',
})
export class StrategyViewComponent {
  readonly view = input<StrategyViewResponse | null>(null);
  readonly loading = input(false);
  readonly failure = input<StrategyViewFailure | null>(null);
  readonly gate = input<StrategyViewGateView | null>(null);
  readonly selectedBarCloseMs = model<number | null>(null);

  readonly retry = output();

  protected readonly colors = GATE_CANDLE_COLORS;
  private readonly popover = signal<{ readonly barCloseMs: number; readonly anchor: PopoverAnchor } | null>(null);

  /** Backend sentences about this read (missing before-start bars, decisions
   * recorded without values), shown verbatim. */
  protected readonly notices = computed(() => this.view()?.notices ?? []);

  protected readonly timeframe = computed(() => {
    const view = this.view();
    return view === null ? null : decisionTimeframeLabel(view.decision_timeframe_ms);
  });

  protected readonly legendLines = computed(() => {
    const view = this.view();
    if (view === null) return [];
    return strategyLinePlans(view.declaration, []).map((plan) => ({
      key: plan.key,
      label: plan.label,
      color: lineColorVar(plan.color),
    }));
  });

  protected readonly openCandle = computed(() => {
    const open = this.popover();
    const candle = candleAt(this.view()?.candles ?? [], open?.barCloseMs ?? null);
    return open === null || candle === null ? null : { candle, anchor: open.anchor };
  });

  protected onCandleClicked(click: StrategyCandleClick): void {
    this.selectedBarCloseMs.set(click.barCloseMs);
    this.popover.set({
      barCloseMs: click.barCloseMs,
      anchor: { left: click.clientX, top: click.clientY, bottom: click.clientY },
    });
  }

  protected onPopoverClosed(): void {
    this.popover.set(null);
  }
}
