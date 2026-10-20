import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

import type { RunFillView } from '../../lib/broker-v2-panel.types';
import { AssetIdentityComponent } from '../../../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../../../shared/timestamp/timestamp-display.component';
import { ReceiptLabelPipe } from '../../../../../shared/pipes/receipt-label.pipe';
import { fmtCurrency } from '../../../format';
import { OptionContractComponent } from '../../../../../shared/option-contract/option-contract.component';
import { optionContractFromSymbol } from '../../../../../shared/option-contract/option-contract-symbol';

/** Shared row presentation for the inline fills rail and the full-history drawer. */
@Component({
  selector: 'app-fill-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, OptionContractComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './fill-table.component.html',
  styleUrl: './fill-table.component.scss',
})
export class FillTableComponent {
  readonly fills = input<readonly RunFillView[]>([]);
  readonly label = input.required<string>();
  /** Say which asset each fill was in: for a bot that trades something other
   * than the symbol it listens to (#2912). An ordinary bot's table has no
   * such column. */
  readonly showAsset = input(false);

  protected readonly rows = computed(() => this.fills().map(fill => ({
    fill, contract: fill.symbol ? optionContractFromSymbol(fill.symbol) : null,
  })));

  protected readonly fmtCurrency = fmtCurrency;
}
