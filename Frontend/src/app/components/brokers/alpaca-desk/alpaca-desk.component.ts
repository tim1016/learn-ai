import { ChangeDetectionStrategy, Component } from '@angular/core';
import { TagModule } from 'primeng/tag';

import { AlpacaAccountCardComponent } from './alpaca-account-card.component';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';
import { AlpacaOrdersTableComponent } from './alpaca-orders-table.component';
import { AlpacaPositionsTableComponent } from './alpaca-positions-table.component';

/**
 * Alpaca broker desk (Broker System v2) — the `/brokers/alpaca` route target.
 * Composes the account card, positions/orders tables, and (phase-2 S1) the
 * order-entry panel. Separate from every v1 broker page.
 */
@Component({
  selector: 'app-alpaca-desk',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaAccountCardComponent,
    AlpacaPositionsTableComponent,
    AlpacaOrdersTableComponent,
    AlpacaOrderEntryComponent,
    TagModule,
  ],
  templateUrl: './alpaca-desk.component.html',
  host: { class: 'block h-full' },
})
export class AlpacaDeskComponent {}
