import { CurrencyPipe, DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { TagModule } from 'primeng/tag';
import { timer } from 'rxjs';

import type {
  SqliteExtendedLimitConfirmation,
  SqliteExtendedLimitPricing,
} from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/** Alpaca's limit precision, for display only: two decimals at or above $1, four below.
 * The Clerk checks the price it would submit; this only renders the suggestion. */
export function formatLimitPrice(price: number): string {
  return price.toFixed(price >= 1 ? 2 : 4);
}

/** A plain positive decimal as typed — no exponent, sign or thousands separator. */
const PLAIN_DECIMAL = /^\d+(\.\d+)?$/;

/** What the operator looked at when they pressed Review: the confirm pane
 * renders this, and Send sends exactly this — never a later refresh. */
interface ReviewedLimit {
  readonly limitText: string;
  readonly limitPrice: number;
  readonly quoteObservedAtMs: number;
  readonly quoteReceivedAtMs: number;
  readonly bid: number;
  readonly ask: number;
  readonly side: SqliteExtendedLimitPricing['side'];
  readonly quantity: number;
}

/**
 * The operator's ticket for an extended-hours safe flatten (#2007).
 *
 * Outside the regular session a flatten is a limit order, and it sells only
 * as well as its price and the book allow. The ticket shows the live IBKR bid,
 * ask, sizes and spread; suggests the Clerk's price (bid less the sealed exit
 * allowance to sell, ask plus it to cover); and warns, before anything is
 * sent, when the spread is wide, when the quantity exceeds the displayed size
 * (the order may fill down to the limit), and how much the limit gives up
 * against the book at worst. A price past the Clerk's band cannot be reviewed.
 *
 * Review freezes the price and the quote beside it; Send emits exactly that.
 * Quote age is measured from when this browser received the quote, so a
 * skewed browser clock cannot make a fresh quote look stale or a stale one
 * fresh; the Clerk re-judges freshness against its own clock regardless.
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
  /** When this browser received ``pricing`` (its own clock). */
  readonly quoteReceivedAtMs = input.required<number>();
  readonly pending = input(false);
  /** The reviewed price and the quote it was reviewed against. */
  readonly send = output<SqliteExtendedLimitConfirmation>();
  readonly refresh = output();

  private readonly injector = inject(Injector);
  private readonly reviewButton = viewChild<ElementRef<HTMLButtonElement>>('reviewButton');
  private readonly sendButton = viewChild<ElementRef<HTMLButtonElement>>('sendButton');
  private readonly editedLimit = signal<string | null>(null);
  protected readonly reviewed = signal<ReviewedLimit | null>(null);
  private readonly displayClock = toSignal(timer(0, 1_000), { initialValue: 0 });

  protected readonly limitText = computed(
    () => this.editedLimit() ?? formatLimitPrice(this.pricing().suggested_limit_price),
  );
  protected readonly limitPrice = computed<number | null>(() => {
    const text = this.limitText().trim();
    if (!PLAIN_DECIMAL.test(text)) return null;
    const value = Number(text);
    return value > 0 ? value : null;
  });
  protected readonly isSell = computed(() => this.pricing().side === 'sell');
  protected readonly phaseLabel = computed(() =>
    this.pricing().phase === 'PRE' ? 'Pre-market' : 'After-hours',
  );
  protected readonly sideLabel = computed(() => (this.isSell() ? 'Sell' : 'Buy to cover'));
  protected readonly bookSideLabel = computed(() => (this.isSell() ? 'bid' : 'ask'));
  /** The price this order must cross to fill now: the bid to sell, the ask to cover. */
  private readonly touch = computed(() =>
    this.isSell() ? this.pricing().bid : this.pricing().ask,
  );
  private readonly touchSize = computed(() =>
    this.isSell() ? this.pricing().bid_size : this.pricing().ask_size,
  );
  protected readonly spread = computed(() => this.pricing().ask - this.pricing().bid);
  protected readonly spreadBps = computed<number | null>(() => {
    const mid = (this.pricing().bid + this.pricing().ask) / 2;
    return mid > 0 ? (this.spread() / mid) * 10_000 : null;
  });
  protected readonly wideSpread = computed(() => {
    const bps = this.spreadBps();
    return bps !== null && bps > this.pricing().spread_warning_bps;
  });
  /** How far the limit reaches through the book past the touch, in bps (negative = resting). */
  protected readonly throughBookBps = computed<number | null>(() => {
    const price = this.limitPrice();
    const touch = this.touch();
    if (price === null || touch <= 0) return null;
    const through = this.isSell() ? touch - price : price - touch;
    return (through / touch) * 10_000;
  });
  /** At worst every share fills at the limit: what that gives up against the touch. */
  protected readonly worstCaseCost = computed<number | null>(() => {
    const price = this.limitPrice();
    if (price === null) return null;
    const through = this.isSell() ? this.touch() - price : price - this.touch();
    return Math.max(0, through) * this.quantity();
  });
  protected readonly outsideBand = computed(() => {
    const price = this.limitPrice();
    if (price === null) return false;
    const band = this.pricing().band_limit_price;
    return this.isSell() ? price < band : price > band;
  });
  protected readonly thinBook = computed(() => {
    const size = this.touchSize();
    return size !== null && this.quantity() > size;
  });
  protected readonly restingNote = computed<string | null>(() => {
    const through = this.throughBookBps();
    if (through === null || through >= 0) return null;
    return this.isSell()
      ? 'Above the bid: this sells only if a buyer pays your price.'
      : 'Below the ask: this covers only if a seller accepts your price.';
  });
  protected readonly quoteAgeSeconds = computed(() => {
    this.displayClock();
    return Math.max(0, Math.floor((Date.now() - this.quoteReceivedAtMs()) / 1_000));
  });
  protected readonly quoteStale = computed(() => {
    this.displayClock();
    return Date.now() - this.quoteReceivedAtMs() > this.pricing().quote_max_age_ms;
  });
  protected readonly canReview = computed(
    () =>
      this.limitPrice() !== null && !this.outsideBand() && !this.quoteStale() && !this.pending(),
  );
  /** Why the reviewed order can no longer be sent as reviewed, or ``null``. */
  protected readonly reviewInvalid = computed<string | null>(() => {
    this.displayClock();
    const reviewed = this.reviewed();
    if (reviewed === null) return null;
    if (reviewed.side !== this.pricing().side || reviewed.quantity !== this.quantity()) {
      return 'The position changed after you reviewed. Review the order again.';
    }
    if (Date.now() - reviewed.quoteReceivedAtMs > this.pricing().quote_max_age_ms) {
      return 'The quote you reviewed is more than '
        + `${this.pricing().quote_max_age_ms / 1_000} seconds old. Review again at the live quote.`;
    }
    return null;
  });

  protected onLimitInput(value: string): void {
    this.editedLimit.set(value);
    this.reviewed.set(null);
  }

  protected useSuggested(): void {
    this.editedLimit.set(null);
    this.reviewed.set(null);
  }

  protected review(): void {
    const limitPrice = this.limitPrice();
    if (limitPrice === null || !this.canReview()) return;
    const pricing = this.pricing();
    this.reviewed.set({
      limitText: this.limitText().trim(),
      limitPrice,
      quoteObservedAtMs: pricing.quote_observed_at_ms,
      quoteReceivedAtMs: this.quoteReceivedAtMs(),
      bid: pricing.bid,
      ask: pricing.ask,
      side: pricing.side,
      quantity: this.quantity(),
    });
    afterNextRender(() => this.sendButton()?.nativeElement.focus(), { injector: this.injector });
  }

  protected cancel(): void {
    this.reviewed.set(null);
    afterNextRender(() => this.reviewButton()?.nativeElement.focus(), { injector: this.injector });
  }

  protected confirmSend(): void {
    const reviewed = this.reviewed();
    if (reviewed === null || this.reviewInvalid() !== null || this.pending()) return;
    this.reviewed.set(null);
    this.send.emit({
      limit_price: reviewed.limitPrice,
      quote_observed_at_ms: reviewed.quoteObservedAtMs,
    });
  }
}
