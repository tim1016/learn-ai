import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, signal } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { equityPlot } from '../run/run-chart-scale';
import type { PositionPreview } from './position-preview.service';

/** Projects Python-authored price and P&L arrays to chart geometry (ADR 0068). */
@Component({
  selector: 'app-options-position-payoff',
  imports: [AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './position-payoff.component.html',
  styleUrl: './position-payoff.component.scss',
})
export class PositionPayoffComponent {
  readonly preview = input.required<PositionPreview>();
  protected readonly showLegs = signal(true);
  protected readonly selected = linkedSignal(() => Math.floor(this.preview().prices_usd.length / 2));
  protected readonly plot = computed(() => {
    const value = this.preview();
    const curves = value.curves.filter((curve) => curve.leg_id === null || this.showLegs());
    const flat = curves.flatMap((curve) => curve.pnl_usd);
    const scale = equityPlot([...flat, '0']);
    if (scale === null || value.prices_usd.length === 0) return null;
    const left = Number(value.prices_usd[0]);
    const right = Number(value.prices_usd.at(-1));
    const xs = value.prices_usd.map((price) => 80 + 900 * (Number(price) - left) / (right - left || 1));
    const count = value.prices_usd.length;
    return {
      low: scale.low, high: scale.high, zero: scale.coordinates.at(-1)?.y ?? 80,
      first: value.prices_usd[0], last: value.prices_usd.at(-1),
      curves: curves.map((curve, index) => ({ ...curve, points: curve.pnl_usd.map((_, i) => `${xs[i]},${scale.coordinates[index * count + i].y}`).join(' ') })),
      selectedX: xs[this.selected()],
    };
  });
  protected readonly inspected = computed(() => {
    const value = this.preview();
    return { price: value.prices_usd[this.selected()], pnl: value.curves[0]?.pnl_usd[this.selected()] };
  });
  protected readonly toggle = (value: boolean): boolean => !value;
  protected inspect(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.selected.set(event.target.valueAsNumber);
  }
}
