import { CurrencyPipe, DecimalPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';
import { Table, TableModule } from 'primeng/table';

import { BrokersService } from '../../../services/brokers.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { AlpacaTraderLensDataService } from './alpaca-trader-lens-data.service';

/**
 * Alpaca open-positions table. Read-only. Four distinct renders: loading,
 * error (couldn't reach Alpaca), honest-empty (no open positions), and data.
 */
@Component({
  selector: 'app-alpaca-positions-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    ButtonModule,
    CurrencyPipe,
    DecimalPipe,
    InputText,
    PercentPipe,
    ReceiptLabelPipe,
    TableModule,
    TimestampDisplayComponent,
  ],
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

  protected readonly globalFilterFields = ['symbol', 'side', 'asset_class'];
  protected readonly tablePassThrough = {
    table: { 'aria-label': 'Current positions' },
  };

  protected inputValue(event: Event): string {
    return event.target instanceof HTMLInputElement ? event.target.value : '';
  }

  protected clearSearch(table: Table, input: HTMLInputElement): void {
    input.value = '';
    table.clear();
  }
}
