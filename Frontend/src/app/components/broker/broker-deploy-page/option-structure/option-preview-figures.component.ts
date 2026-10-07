import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { formatAuthoredUsd } from '../../../../shared/pipes/authored-usd.pipe';
import type { OptionStructurePreview } from '../../v2-panel/lib/broker-v2-panel.service';
import { usdFromCents } from './option-preview-display';

/**
 * The figures of a resolved option structure preview (#2915): which way it
 * leans, the at-expiry estimate, and the money. Every sentence is the
 * server's, and every amount is one it sent in whole cents, written out and
 * never added up here. The premium's size is shown beside the server's word
 * for its direction (`premium_is`), so "received" never reads as a minus.
 *
 * `compact` is Deploy's short view: the premium a share and a structure,
 * the lean and the estimate, and the cost of opening and closing at once
 * only when it is over the owner's limit.
 */
@Component({
  selector: 'app-option-preview-figures',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './option-preview-figures.component.html',
  styleUrl: './option-preview-figures.component.scss',
})
export class OptionPreviewFiguresComponent {
  /** A resolved answer. */
  readonly preview = input.required<OptionStructurePreview>();
  readonly compact = input(false);

  protected readonly money = computed(() => {
    const money = this.preview().money;
    if (money === null) return null;
    // The net price a share, as the server wrote it, without the sign its word carries.
    const price = money.net_price.replace(/^-/, '');
    return {
      structures: money.structures,
      price: formatAuthoredUsd(price) ?? price,
      premium: usdFromCents(Math.abs(money.net_entry_premium_cents)),
      premiumEach: usdFromCents(Math.abs(money.net_entry_premium_per_structure_cents)),
      // "paid" or "received" as the server says it; a premium of nothing needs no word.
      premiumIs: money.premium_is === 'nothing' ? '' : money.premium_is,
      requirement: usdFromCents(money.broker_requirement_cents),
      fees: usdFromCents(money.fee_estimate_cents),
      setAside: usdFromCents(money.entry_commitment_cents),
      roundTrip: usdFromCents(money.round_trip_cost_cents),
      roundTripPercent: money.round_trip_cost_percent,
      overLimit: money.over_cost_limit,
      costSentence: money.cost_sentence,
      feesSentence: money.fees_sentence,
    };
  });
}
