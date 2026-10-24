import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import {
  ADD_LEG_UNAVAILABLE_REASON,
  CHAIN_COLUMN_LABELS,
  chainAbsentWords,
  type ChainField,
  type ChainTable,
} from './chain-presentation';

/**
 * The one chain grid (PRD #3490 R11): calls on the left, puts on the right, the strike
 * between. It draws what the presentation carries and decides nothing: which columns exist,
 * whether a value has a time, and why a cell is empty are the source adapter's, said in the
 * table's own types.
 *
 * A source that dates each value and says why one is missing (`full`) draws its price with the
 * minute it is the close of, or the reason in words. A source with many narrow columns
 * (`compact`) draws a dash for what the provider sent as nothing, with the reason as its title.
 *
 * Add leg stays disabled with its reason: a contract fixes strike and expiry but carries no
 * timing rules, so it cannot become a setup faithfully (PRD #3490 D04).
 */
@Component({
  selector: 'app-chain-grid',
  imports: [NgTemplateOutlet, AssetIdentityComponent, TimestampDisplayComponent],
  templateUrl: './chain-grid.component.html',
  styleUrl: './chain-grid.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChainGridComponent {
  readonly table = input.required<ChainTable>();
  /** The underlying the contracts are of, which names an option's symbol. */
  readonly underlying = input.required<string>();

  protected readonly addLegReason = ADD_LEG_UNAVAILABLE_REASON;
  protected readonly labels = CHAIN_COLUMN_LABELS;

  protected readonly callSpan = computed(() => this.table().columns.calls.length + 1);
  protected readonly putSpan = computed(() => this.table().columns.puts.length + 1);
  protected readonly hasItm = computed(() => this.table().rows.some((row) => row.itm !== null));
  protected readonly hasAtm = computed(() => this.table().rows.some((row) => row.atm));

  protected wordsOf(field: Extract<ChainField, { kind: 'absent' }>): string {
    return chainAbsentWords(field);
  }
}
