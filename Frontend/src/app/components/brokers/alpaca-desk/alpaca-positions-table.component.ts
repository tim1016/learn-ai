import { CurrencyPipe, DecimalPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { AlpacaTraderLensDataService } from './alpaca-trader-lens-data.service';

/**
 * Alpaca open-positions table. Read-only. Four distinct renders: loading,
 * error (couldn't reach Alpaca), honest-empty (no open positions), and data.
 */
@Component({
  selector: 'app-alpaca-positions-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, CurrencyPipe, DecimalPipe, PercentPipe, ReceiptLabelPipe],
  templateUrl: './alpaca-positions-table.component.html',
  styleUrl: './alpaca-positions-table.component.scss',
  host: { class: 'block' },
})
export class AlpacaPositionsTableComponent {
  private readonly traderData = inject(AlpacaTraderLensDataService, { optional: true });
  private readonly brokers = inject(BrokersService);

  protected readonly positions = this.traderData?.positions ?? resource({
    loader: () => this.brokers.listPositions(),
  });
}
