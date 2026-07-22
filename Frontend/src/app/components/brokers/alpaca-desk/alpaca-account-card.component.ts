import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { BrokersService } from '../../../services/brokers.service';

/**
 * Alpaca account summary card (equity / cash / buying power / status).
 * Read-only. Loading and error are distinct renders; there is no "empty"
 * state — a paper account always exists.
 */
@Component({
  selector: 'app-alpaca-account-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-account-card.component.html',
  styleUrl: './alpaca-account-card.component.scss',
})
export class AlpacaAccountCardComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly account = resource({
    loader: () => this.brokers.getAccount(),
  });
}
