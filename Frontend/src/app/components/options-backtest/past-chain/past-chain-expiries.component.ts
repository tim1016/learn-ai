import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { ExpiryTagComponent } from './expiry-tag.component';
import type { DayView } from './past-chain-view';

/**
 * What the listing held on the picked date: its state in words when there is nothing to list (not a trading day, not
 * stored, cannot be read, holds nothing a backtest can use), or every expiry in order, each with its tag and, apart
 * from it, whether it expires today. Picking an expiry is the owner's act and nothing here picks one.
 */
@Component({
  selector: 'app-options-backtest-past-chain-expiries',
  imports: [ReceiptLabelPipe, TimestampDisplayComponent, ExpiryTagComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './past-chain-expiries.component.html',
  styleUrls: ['../options-backtest-card.scss', './past-chain.scss'],
})
export class PastChainExpiriesComponent {
  readonly view = input.required<DayView>();
  /** The `expiry_ms` of the expiry whose chain is open, which may be one this date does not list. */
  readonly picked = input<number | null>(null);
  /** The listing on screen is the last answer, while the same question is read again. */
  readonly again = input(false);
  readonly pick = output<number>();
  readonly retry = output();

  /** A row's date and its button name each other: the button reads as "<date> Show chain". */
  protected dateId(expiryMs: number): string {
    return `ob-chain-expiry-${expiryMs}`;
  }

  protected buttonId(expiryMs: number): string {
    return `ob-chain-show-${expiryMs}`;
  }

  protected nameOf(expiryMs: number): string {
    return `${this.dateId(expiryMs)} ${this.buttonId(expiryMs)}`;
  }
}
