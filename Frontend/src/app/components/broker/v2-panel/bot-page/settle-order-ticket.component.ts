import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { FormField, disabled, form, required } from '@angular/forms/signals';

import type {
  SqliteAttestationFindings,
  SqliteRecoveryActionCheck,
  SqliteSettlementChoice,
  SqliteUnknownOrderAttestation,
  SqliteUnknownOrderSettlement,
} from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotEndRefusalComponent } from '../../bot-end/bot-end-refusal.component';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import type { PanelAction } from '../lib/broker-v2-panel.types';
import { deriveActionRejection, type ActionRejection } from '../lib/panel-action-outcome';

/** The settle action while it offers an order to settle: the one action a press of which opens this ticket. */
export type SettleAction = PanelAction & { readonly settlement: SqliteUnknownOrderSettlement };

/** Whether `action` offers an order to settle. The page asks this once and passes the answer down. */
export function offersSettlement(action: PanelAction): action is SettleAction {
  return action.settlement != null;
}

/** What the operator has said so far: which of the backend's choices, and the Alpaca order id typed for it. */
interface SettleAnswer {
  readonly attestation: '' | SqliteSettlementChoice['attestation'];
  readonly orderId: string;
}

const NOTHING_SAID: SettleAnswer = { attestation: '', orderId: '' };

/** What the Clerk answered, and what it was asked about: the action as it was
 * presented then -- its token names the order's latest send -- and what was said of it. */
interface Answered<T> {
  readonly token: string;
  readonly attestation: SqliteUnknownOrderAttestation;
  readonly value: T;
}

/** What the ticket reads of the Clerk's check: what it found for the attestation the check carried. */
export type SettlementCheck = Pick<SqliteRecoveryActionCheck, 'attestation_findings'>;

/** Said only when a request came back with no words of the backend's own. */
const CHECK_UNANSWERED = 'The check did not finish. Nothing was changed.';
const SETTLE_UNANSWERED = 'No answer came back, so the result is not known.';

let nextTicketId = 0;

function sameAttestation(left: SqliteUnknownOrderAttestation, right: SqliteUnknownOrderAttestation): boolean {
  return left.kind === right.kind && (left.broker_order_id ?? null) === (right.broker_order_id ?? null);
}

/** A settlement that did not go through, in the backend's words; when no answer came back, that is what is said. */
export function settleRefusal(error: unknown): ActionRejection {
  return deriveActionRejection(error, SETTLE_UNANSWERED);
}

/**
 * The ticket that settles a bot's uncertain closing order (#2854).
 *
 * The Clerk sent a closing order and could not confirm it. The operator looks
 * at the account at Alpaca and says what it shows, from the choices the
 * backend offers: Alpaca has no such order, or it shows it under an Alpaca
 * order id. Nothing is taken on their word. The page first asks the Clerk to
 * check -- it reads Alpaca itself and says what it found -- and only an
 * answer that would settle the order can be confirmed, as the backend's
 * confirmation asks. The Clerk reads Alpaca again when it settles.
 *
 * Every sentence is the backend's: the ticket, each choice, what the Clerk
 * found, the confirmation and its word, and a refusal. This component adds
 * the names of its controls. What the Clerk found, and a refusal, belong to
 * what they were asked about -- this send of the order, and what was said of
 * it: change the choice or the id and they are gone, so the operator can go
 * back and say the other thing; an answer that lands after the page presented
 * the action anew is never shown. Each answer takes the keyboard when it
 * arrives.
 *
 * `check` and `settle` are the page's own requests, as the end card's `save`
 * is: fenced to the lane the ticket was opened on, one at a time. `settle`
 * resolves once the page has shown the answer itself -- the order settled
 * and the ticket closed, or a refusal that came back after the ticket left
 * this send of the order -- and rejects with a refusal for the ticket to
 * show. The Alpaca order id is trimmed and sent as typed: the backend says
 * whether it is one.
 */
@Component({
  selector: 'app-settle-order-ticket',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndRefusalComponent, FormField, TimestampDisplayComponent, TypedHaltConfirmComponent],
  templateUrl: './settle-order-ticket.component.html',
  styleUrl: './settle-order-ticket.component.scss',
})
export class SettleOrderTicketComponent {
  /** The settle action as the page presents it for this ticket: its order, its choices, its confirmation. */
  readonly action = input.required<SettleAction>();
  /** The account the order is on, shown by its number in the confirmation. */
  readonly accountId = input<string | null>(null);
  /** Asks the Clerk what it reads at Alpaca for what the operator said. Settles nothing; rejects with a refusal. */
  readonly check = input.required<(attestation: SqliteUnknownOrderAttestation) => Promise<SettlementCheck>>();
  /** Settles the order on what the operator said; rejects with the refusal this ticket is to show. */
  readonly settle = input.required<(attestation: SqliteUnknownOrderAttestation) => Promise<void>>();
  /** Another command on this bot is on its way. */
  readonly locked = input(false);

  readonly closed = output();

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);
  private readonly id = nextTicketId++;
  protected readonly titleId = `settle-ticket-${this.id}-title`;
  protected readonly foundId = `settle-ticket-${this.id}-found`;

  protected readonly confirmation = computed(() => this.action().confirmation ?? null);

  protected readonly checking = signal(false);
  protected readonly settling = signal(false);
  /** The confirmation is of what the Clerk found for the action as presented: presented again, it closes. */
  protected readonly confirming = linkedSignal({ source: this.action, computation: () => false });
  protected readonly busy = computed(() => this.checking() || this.settling() || this.locked());

  /** A ticket presented again is about the order as it stands now: nothing said before carries over. */
  protected readonly answer = linkedSignal({ source: this.action, computation: (): SettleAnswer => NOTHING_SAID });

  private readonly chosen = computed(
    () => this.action().settlement.choices.find((choice) => choice.attestation === this.answer().attestation) ?? null,
  );

  /** Whether the chosen answer names the order by its Alpaca order id. */
  protected readonly asksOrderId = computed(() => this.chosen()?.asks_broker_order_id === true);

  protected readonly answerForm = form(this.answer, (answer) => {
    disabled(answer.attestation, { when: () => this.busy() });
    disabled(answer.orderId, { when: () => this.busy() });
    required(answer.orderId, { when: () => this.asksOrderId() });
  });

  /** What the operator has said, ready to send -- or `null` while a choice or its order id is missing. */
  protected readonly attestation = computed((): SqliteUnknownOrderAttestation | null => {
    const choice = this.chosen();
    if (choice === null) return null;
    if (!choice.asks_broker_order_id) return { kind: choice.attestation };
    const orderId = this.answer().orderId.trim();
    return orderId === '' ? null : { kind: choice.attestation, broker_order_id: orderId };
  });

  private readonly lastFound = linkedSignal({
    source: this.action, computation: (): Answered<SqliteAttestationFindings> | null => null,
  });
  private readonly lastRefusal = linkedSignal({
    source: this.action, computation: (): Answered<ActionRejection> | null => null,
  });

  /** What the Clerk read for exactly what is said now. */
  protected readonly found = computed(() => this.current(this.lastFound()));
  /** The refusal of exactly what is said now. */
  protected readonly refusal = computed(() => this.current(this.lastRefusal()));

  /** The Clerk's findings as the confirmation says them: its own sentences, in its order. */
  protected readonly foundText = computed(() => this.found()?.lines.join(' ') ?? '');

  private readonly foundView = viewChild<ElementRef<HTMLElement>>('foundBox');
  private readonly refusalView = viewChild<unknown, ElementRef<HTMLElement>>('refusalBox', { read: ElementRef });

  /** Moves the keyboard to the ticket, e.g. when the toolbar opens it. */
  focus(): void {
    this.host.nativeElement.querySelector<HTMLElement>('section')?.focus();
  }

  protected async runCheck(): Promise<void> {
    const attestation = this.attestation();
    if (attestation === null || this.busy()) return;
    const asked = { token: this.action().concurrency_token, attestation };
    this.checking.set(true);
    this.lastFound.set(null);
    this.lastRefusal.set(null);
    try {
      const findings = (await this.check()(attestation)).attestation_findings ?? null;
      if (findings === null) {
        this.lastRefusal.set({ ...asked, value: deriveActionRejection(null, CHECK_UNANSWERED) });
      } else {
        this.lastFound.set({ ...asked, value: findings });
      }
    } catch (error) {
      this.lastRefusal.set({ ...asked, value: deriveActionRejection(error, CHECK_UNANSWERED) });
    } finally {
      this.checking.set(false);
      this.focusAnswer();
    }
  }

  /** Settle on exactly what the Clerk checked; nothing while what it found would not settle the order. */
  protected async runSettle(): Promise<void> {
    this.confirming.set(false);
    const attestation = this.attestation();
    if (attestation === null || this.busy() || this.found()?.would_settle !== true) return;
    const asked = { token: this.action().concurrency_token, attestation };
    this.settling.set(true);
    this.lastRefusal.set(null);
    try {
      await this.settle()(attestation);
    } catch (error) {
      this.lastRefusal.set({ ...asked, value: settleRefusal(error) });
      this.focusAnswer();
    } finally {
      this.settling.set(false);
    }
  }

  /** An answer is shown only for what it was asked about: the action as presented now, and what is said now. */
  private current<T>(answered: Answered<T> | null): T | null {
    const attestation = this.attestation();
    return answered !== null
      && attestation !== null
      && answered.token === this.action().concurrency_token
      && sameAttestation(answered.attestation, attestation)
      ? answered.value
      : null;
  }

  /** The Clerk's answer takes the keyboard once it is on screen: the button
   * that asked is disabled while it waits, and a confirmation is gone once
   * confirmed, so the keyboard would otherwise be left on the page's body.
   * An answer that is not shown -- the page presented the action anew
   * meanwhile -- leaves the keyboard on the ticket. */
  private focusAnswer(): void {
    // The page may have closed the ticket while it waited: nothing is shown, so nothing takes the keyboard.
    if (this.destroyRef.destroyed) return;
    afterNextRender(() => {
      const answer = this.refusalView() ?? this.foundView();
      if (answer === undefined) this.focus();
      else answer.nativeElement.focus();
    }, { injector: this.injector });
  }
}
