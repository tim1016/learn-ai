import { ChangeDetectionStrategy, Component } from '@angular/core';
import { TagModule } from 'primeng/tag';

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
    TagModule,
  ],
  templateUrl: './alpaca-desk.component.html',
  host: { class: 'block h-full' },
})
export class AlpacaDeskComponent {}
