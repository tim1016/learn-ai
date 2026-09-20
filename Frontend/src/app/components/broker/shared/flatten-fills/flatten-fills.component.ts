import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { RecentFillView } from '../../v2-panel/lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/** One fill of an operator-priced flatten, with what it gave up against the quote. */
interface FlattenFillRow {
  readonly key: string;
  readonly filledAtMs: number;
  readonly side: string;
  readonly quantity: number;
  readonly price: number;
  readonly referencePrice: number;
  readonly slippageBps: number;
  /** Signed dollars against the reference: positive = worse than it. */
  readonly slippageCost: number;
}

/**
 * What an operator's extended-hours flatten actually got (#2007).
 *
 * The Clerk records the live bid (sell) or ask (cover) it priced the
 * confirmed limit against, so each fill can be read against the quote the
 * operator was looking at rather than against nothing at all. Positive
 * slippage is worse than that reference.
 */
@Component({
  selector: 'app-flatten-fills',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DecimalPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  template: `
    <section class="grid gap-2" aria-label="Flatten fills and slippage">
      <h4 class="m-0 eyebrow-heading">Flatten fills</h4>
      <table class="w-full text-sm">
        <caption class="text-left text-xs text-[var(--text-secondary)]">
          Slippage is measured from the bid (sell) or ask (cover) the limit was priced
          against — yours or the Clerk's; positive is worse than it.
        </caption>
        <thead>
          <tr>
            <th scope="col" class="text-left">Time</th>
            <th scope="col" class="text-left">Side</th>
            <th scope="col" class="text-right">Qty</th>
            <th scope="col" class="text-right">Price</th>
            <th scope="col" class="text-right">Reference</th>
            <th scope="col" class="text-right">Slippage</th>
          </tr>
        </thead>
        <tbody>
          @for (row of rows(); track row.key) {
            <tr>
              <td><app-timestamp-display [value]="row.filledAtMs" mode="local" granularity="time" /></td>
              <td>{{ row.side | receiptLabel }}</td>
              <td class="text-right tabular-nums">{{ row.quantity }}</td>
              <td class="text-right tabular-nums">{{ row.price | currency: 'USD' : 'symbol' : '1.2-4' }}</td>
              <td class="text-right tabular-nums">{{ row.referencePrice | currency: 'USD' : 'symbol' : '1.2-4' }}</td>
              <td class="text-right tabular-nums">
                {{ row.slippageBps | number: '1.1-1' }} bps ({{ row.slippageCost | currency: 'USD' }})
              </td>
            </tr>
          }
        </tbody>
      </table>
    </section>
  `,
})
export class FlattenFillsComponent {
  readonly fills = input.required<readonly RecentFillView[]>();

  protected readonly rows = computed<readonly FlattenFillRow[]>(() =>
    this.fills().flatMap((fill, index) => {
      const {
        slippage_bps: bps,
        slippage_cost: cost,
        slippage_reference_price: reference,
        price,
        quantity,
      } = fill;
      if (
        bps === null || bps === undefined || cost === null || cost === undefined
        || !reference || price === null || quantity === null
      ) {
        return [];
      }
      return [{
        // Two partial fills of one order can share a millisecond and a price;
        // only the Clerk's execution identity tells them apart.
        key: fill.event_key ?? `${fill.order_ref}:${index}`,
        filledAtMs: fill.filled_at_ms,
        side: fill.side,
        quantity,
        price,
        referencePrice: reference,
        slippageBps: bps,
        slippageCost: cost,
      }];
    }),
  );
}
