import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { botHues } from '../v2-panel/lib/bot-hue';
import type { AccountMoneyView, MoneySegment } from '../v2-panel/lib/broker-v2-panel.service';

/** Where the bar is drawn: an account's Home, a Deploy or bot page, a bot
 * row's budget strip, or an Accounts card / Wall tile. */
export type MoneyBarSize = 'home' | 'detail' | 'row' | 'mini';

/** What an account's bar carries beside it, never as a slice (PRD #2560 D6):
 * what the claims exceed the account by, when they do, and its open P&L — or
 * the backend's reason there is none. */
export type MoneyBarNotes = Pick<AccountMoneyView, 'account_shortfall_usd' | 'open_pnl_usd' | 'open_pnl_detail'>;

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
 * stopped striped; outside dotted; charges solid grey; settling upright
 * pinstripes; NEW marching; free dashed — and the legend names each slice
 * with its amount, visibly at the `home` and `detail` sizes and for assistive
 * technology at every size. A part the backend did not send (a shortfall
 * where nothing is short) is not stated at all.
 */
@Component({
  selector: 'app-money-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe],
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
  /** The account's notes beside the bar — pass the `AccountMoneyView` itself.
   * Each one is stated only when the backend sent it. */
  readonly notes = input<MoneyBarNotes | null>(null);

  protected readonly legendVisible = computed(() => this.size() === 'home' || this.size() === 'detail');

  protected readonly slices = computed<readonly DrawnSlice[]>(() => {
    const segments = this.segments();
    const hues = botHues(segments);
    return segments.map((segment, index) => ({
      key: `${segment.kind}:${segment.strategy_instance_id ?? ''}`,
      segment,
      hue: hues[index],
    }));
  });
}
