import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { ExpiryTag } from './past-chain.types';

/**
 * An expiry's tag: the series the server decided it belongs to as a small pill that always carries its word (Daily,
 * Weekly, Monthly, Quarterly, Unknown), and, apart from it, a badge when the expiry is the trading date itself. A
 * Monthly on its last day is a Monthly that expires today, never a Daily.
 */
@Component({
  selector: 'app-options-backtest-expiry-tag',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="pill" [attr.data-series]="tag().series">{{ tag().series | receiptLabel }}</span>@if (tag().expires_today) { <span class="badge">Expires today</span> }@if (evidence()) { <span class="evidence">{{ tag().evidence }}</span> }`,
  styleUrl: './past-chain.scss',
})
export class ExpiryTagComponent {
  readonly tag = input.required<ExpiryTag>();
  /** Show the server's words for which rule decided the series. */
  readonly evidence = input(false);
}
