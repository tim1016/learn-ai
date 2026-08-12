import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource, signal } from '@angular/core';
import { TagModule } from 'primeng/tag';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';

/**
 * Alpaca account summary card (equity / cash / buying power / status).
 * Read-only. Loading and error are distinct renders; there is no "empty"
 * state — a paper account always exists.
 */
@Component({
  selector: 'app-alpaca-account-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, ReceiptLabelPipe, TagModule, TimestampDisplayComponent],
  templateUrl: './alpaca-account-card.component.html',
  styleUrl: './alpaca-account-card.component.scss',
  host: { class: 'block' },
})
export class AlpacaAccountCardComponent {
  private readonly deskAccount = inject(AlpacaDeskAccountDataService, { optional: true });
  private readonly brokers = inject(BrokersService);

  protected readonly expanded = signal(false);

  protected readonly account = this.deskAccount?.account ?? resource({
    loader: () => this.brokers.getAccount(),
  });
}
