import { ChangeDetectionStrategy, Component } from '@angular/core';

import { AlpacaAccountCardComponent } from './alpaca-account-card.component';
import { AlpacaOrdersTableComponent } from './alpaca-orders-table.component';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';

/**
 * Alpaca broker desk (Broker System v2) — the `/brokers/alpaca` route target.
 * Read-only; composes the account card (and, in later slices, the positions
 * and orders tables). Separate from every v1 broker page.
 */
@Component({
  selector: 'app-alpaca-desk',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaAccountCardComponent,
    AlpacaPositionsTableComponent,
    AlpacaOrdersTableComponent,
  ],
  templateUrl: './alpaca-desk.component.html',
  styleUrl: './alpaca-desk.component.scss',
})
export class AlpacaDeskComponent {}
