import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';
import { TableModule } from 'primeng/table';

import { BrokersService } from '../../../services/brokers.service';

/**
 * Alpaca open-positions table. Read-only. Four distinct renders: loading,
 * error (couldn't reach Alpaca), honest-empty (no open positions), and data.
 */
@Component({
  selector: 'app-alpaca-positions-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DecimalPipe, TableModule],
  templateUrl: './alpaca-positions-table.component.html',
  host: { class: 'block' },
})
export class AlpacaPositionsTableComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly positions = resource({
    loader: () => this.brokers.listPositions(),
  });
}
