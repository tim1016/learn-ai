import { CurrencyPipe, DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
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
  SqliteProposedLimitEvaluation,
} from '../../../../api/alpaca.types';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';

/** Alpaca's limit precision, for display only: two decimals at or above $1, four below.
 * The Clerk checks the price it would submit; this only renders the suggestion. */
export function formatLimitPrice(price: number): string {
  return price.toFixed(price >= 1 ? 2 : 4);
}

/** A plain positive decimal as typed — no exponent, sign or thousands separator. */
const PLAIN_DECIMAL = /^\d+(\.\d+)?$/;

/** What the operator looked at when the Clerk priced their review: the confirm
 * pane renders this, and Send sends exactly this — never a later refresh. */
interface ReviewedLimit {
  readonly limitText: string;
  readonly limitPrice: number;
  readonly quoteObservedAtMs: number;
  readonly quoteReceivedAtMs: number;
  readonly bid: number;
  readonly ask: number;
  readonly side: SqliteExtendedLimitPricing['side'];
  readonly quantity: number;
  readonly evaluation: SqliteProposedLimitEvaluation;
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
 * against the book at worst.
 *
 * Every one of those numbers is the Clerk's. Pressing Review asks the Clerk
 * what the operator's own price would do against the quote it holds, and the
 * answer is what the confirm pane renders and Send sends; this component
 * derives no execution or cost figure of its own (AGENTS.md § "Python owns
 * all math"). A price the Clerk refuses as past its band never reaches the
 * confirm pane.
 *
 * Quote age is measured from when this browser received the quote, so a
 * skewed browser clock cannot make a fresh quote look stale or a stale one
 * fresh; the Clerk re-judges freshness against its own clock regardless.
 */
@Component({
  selector: 'app-extended-flatten-ticket',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
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
  /** Ask the Clerk what this price would do against the quote it holds. */
  readonly priceCheck = output<number>();

  private readonly injector = inject(Injector);
  private readonly reviewButton = viewChild<ElementRef<HTMLButtonElement>>('reviewButton');
  private readonly sendButton = viewChild<ElementRef<HTMLButtonElement>>('sendButton');
  private readonly editedLimit = signal<string | null>(null);
  /** The price the Clerk was asked to evaluate, until its answer arrives. */
  private readonly awaitingPrice = signal<number | null>(null);
  protected readonly reviewed = signal<ReviewedLimit | null>(null);
  private readonly displayClock = toSignal(timer(0, 1_000), { initialValue: 0 });

  constructor() {
    // The Clerk's answer to a Review is what the confirm pane shows, so it is
    // frozen the moment it lands: a later quote refresh must not move the
    // numbers under the operator, and a price the Clerk put past its band
    // never becomes reviewable at all.
    effect(() => {
      const evaluation = this.pricing().proposal;
      const wanted = this.awaitingPrice();
      if (wanted === null || !evaluation || evaluation.limit_price !== wanted) return;
      untracked(() => {
        this.awaitingPrice.set(null);
        if (evaluation.outside_band) return;
        const pricing = this.pricing();
        this.reviewed.set({
          limitText: formatLimitPrice(wanted),
          limitPrice: wanted,
          quoteObservedAtMs: pricing.quote_observed_at_ms,
          quoteReceivedAtMs: this.quoteReceivedAtMs(),
          bid: pricing.bid,
          ask: pricing.ask,
          side: pricing.side,
          quantity: this.quantity(),
          evaluation,
        });
        afterNextRender(() => this.sendButton()?.nativeElement.focus(), {
          injector: this.injector,
        });
      });
    });
  }

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
  protected readonly touchSize = computed(() =>
    this.isSell() ? this.pricing().bid_size : this.pricing().ask_size,
  );
  /** The Clerk's reading of the price it was last asked about, if it is still current. */
  protected readonly evaluation = computed<SqliteProposedLimitEvaluation | null>(() => {
    const proposal = this.pricing().proposal;
    return proposal && proposal.limit_price === this.limitPrice() ? proposal : null;
  });
  protected readonly checking = computed(() => this.awaitingPrice() !== null);
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
      this.limitPrice() !== null && !this.quoteStale() && !this.pending() && !this.checking(),
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
    this.awaitingPrice.set(null);
    this.reviewed.set(null);
  }

  protected useSuggested(): void {
    this.editedLimit.set(null);
    this.awaitingPrice.set(null);
    this.reviewed.set(null);
  }

  protected review(): void {
    const limitPrice = this.limitPrice();
    if (limitPrice === null || !this.canReview()) return;
    this.awaitingPrice.set(limitPrice);
    this.priceCheck.emit(limitPrice);
  }

  protected cancel(): void {
    this.reviewed.set(null);
    this.awaitingPrice.set(null);
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
