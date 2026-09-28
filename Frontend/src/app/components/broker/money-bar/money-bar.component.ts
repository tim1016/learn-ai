import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { MoneySegment } from '../v2-panel/lib/broker-v2-panel.service';

/** Where the bar is drawn: an account's Home, a Deploy or bot page, a bot
 * row's budget strip, or an Accounts card / Wall tile. */
export type MoneyBarSize = 'home' | 'detail' | 'row' | 'mini';

/** Bot slices take their hue by their place among the bot slices. Colour is
 * never the only carrier: the legend names every slice with its amount. */
const BOT_HUES = [
  'var(--chart-series-blue)',
  'var(--chart-series-amber)',
  'var(--chart-series-pink)',
  'var(--chart-series-green)',
  'var(--chart-series-purple)',
  'var(--chart-series-orange)',
] as const;

interface DrawnSlice {
  readonly key: string;
  readonly segment: MoneySegment;
  readonly hue: string | null;
}

/**
 * The money bar (PRD #2560 D12): one account's money as disjoint slices.
 *
 * Purely presentational and free of arithmetic. Python authors every dollar
 * string and every width (`share_bps`, and a bot slice's `*_bps` shading);
 * each slice is drawn as `flex: <bps> 1 0`, so proportions come from the
 * layout engine with no percentage computed here. Every kind has its own
 * pattern — bot slices shaded for shares, entry orders and free budget;
 * stopped striped; outside dotted; charges solid grey; NEW marching; free
 * dashed — and the legend names each slice with its amount, visibly at the
 * `home` and `detail` sizes and for assistive technology at every size.
 */
@Component({
  selector: 'app-money-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe],
  templateUrl: './money-bar.component.html',
  styleUrl: './money-bar.component.scss',
  host: { '[attr.data-size]': 'size()' },
})
export class MoneyBarComponent {
  /** The segments of an `AccountMoneyView`, in its display order. */
  readonly segments = input.required<readonly MoneySegment[]>();
  readonly size = input<MoneyBarSize>('detail');
  /** The legend's accessible name. */
  readonly caption = input('Where the money is');

  protected readonly legendVisible = computed(() => this.size() === 'home' || this.size() === 'detail');

  protected readonly slices = computed<readonly DrawnSlice[]>(() => {
    let bots = 0;
    return this.segments().map((segment) => ({
      key: `${segment.kind}:${segment.strategy_instance_id ?? ''}`,
      segment,
      hue: segment.kind === 'bot' ? BOT_HUES[bots++ % BOT_HUES.length] : null,
    }));
  });
}
