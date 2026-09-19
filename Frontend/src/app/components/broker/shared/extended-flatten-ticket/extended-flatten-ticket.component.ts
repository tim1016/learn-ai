import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { TagModule } from 'primeng/tag';
import { timer } from 'rxjs';

import type { SqliteExtendedLimitPricing } from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/** Alpaca's limit precision: two decimals at or above $1, four below. */
function formatLimit(price: number): string {
  return price.toFixed(price >= 1 ? 2 : 4);
}

/**
 * The operator's ticket for an extended-hours safe flatten (#2007).
 *
 * Outside the regular session a flatten is a limit order, and it sells only
 * as well as its price. The ticket shows the live IBKR bid, ask and spread,
 * suggests the Clerk's price (bid less the sealed exit allowance to sell, ask
 * plus it to cover), and lets the operator send that or their own price —
 * only after an explicit confirm that names side, quantity and limit.
 *
 * The limit follows the live suggestion until the operator edits it, and
 * then keeps their value across quote refreshes. A quote older than the
 * server's bound cannot be sent against: the Clerk would refuse it.
 */
@Component({
  selector: 'app-extended-flatten-ticket',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ButtonModule,
    CurrencyPipe,
    DecimalPipe,
    InputText,
    MessageModule,
    TagModule,
    TimestampDisplayComponent,
  ],
  templateUrl: './extended-flatten-ticket.component.html',
})
export class ExtendedFlattenTicketComponent {
  readonly pricing = input.required<SqliteExtendedLimitPricing>();
  readonly quantity = input.required<number>();
  readonly pending = input(false);
  /** The confirmed limit price, after the operator's explicit confirm. */
  readonly send = output<number>();
  readonly refresh = output();

  private readonly editedLimit = signal<string | null>(null);
  protected readonly confirming = signal(false);
  private readonly displayClock = toSignal(timer(0, 1_000), { initialValue: 0 });

  protected readonly limitText = computed(
    () => this.editedLimit() ?? formatLimit(this.pricing().suggested_limit_price),
  );
  protected readonly limitPrice = computed<number | null>(() => {
    const value = Number(this.limitText());
    return Number.isFinite(value) && value > 0 ? value : null;
  });
  protected readonly spread = computed(() => this.pricing().ask - this.pricing().bid);
  protected readonly spreadBps = computed<number | null>(() => {
    const mid = (this.pricing().bid + this.pricing().ask) / 2;
    return mid > 0 ? (this.spread() / mid) * 10_000 : null;
  });
  protected readonly quoteAgeSeconds = computed(() => {
    this.displayClock();
    return Math.max(0, Math.floor((Date.now() - this.pricing().quote_observed_at_ms) / 1_000));
  });
  protected readonly quoteStale = computed(() => {
    this.displayClock();
    return Date.now() - this.pricing().quote_observed_at_ms > this.pricing().quote_max_age_ms;
  });
  protected readonly phaseLabel = computed(() =>
    this.pricing().phase === 'PRE' ? 'Pre-market' : 'After-hours',
  );
  protected readonly sideLabel = computed(() =>
    this.pricing().side === 'sell' ? 'Sell' : 'Buy to cover',
  );
  protected readonly anchorLabel = computed(() =>
    this.pricing().side === 'sell' ? 'bid −' : 'ask +',
  );
  /** Where the limit sits against the book it must cross to fill now. */
  protected readonly fillNote = computed<string | null>(() => {
    const price = this.limitPrice();
    if (price === null) return null;
    const { side, bid, ask } = this.pricing();
    if (side === 'sell' && price > bid) {
      return 'Above the bid: this sells only if a buyer pays your price.';
    }
    if (side === 'buy' && price < ask) {
      return 'Below the ask: this covers only if a seller accepts your price.';
    }
    return null;
  });
  protected readonly canSend = computed(
    () => this.limitPrice() !== null && !this.quoteStale() && !this.pending(),
  );

  protected onLimitInput(value: string): void {
    this.editedLimit.set(value);
    this.confirming.set(false);
  }

  protected useSuggested(): void {
    this.editedLimit.set(null);
    this.confirming.set(false);
  }

  protected review(): void {
    if (this.canSend()) this.confirming.set(true);
  }

  protected cancel(): void {
    this.confirming.set(false);
  }

  protected confirmSend(): void {
    const price = this.limitPrice();
    if (price === null || !this.canSend()) return;
    this.confirming.set(false);
    this.send.emit(price);
  }
}
