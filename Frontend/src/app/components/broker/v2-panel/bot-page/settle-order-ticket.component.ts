import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';
import { FormField, disabled, form, required } from '@angular/forms/signals';

import type {
  SqliteAttestationFindings,
  SqliteRecoveryActionCheck,
  SqliteSettlementChoice,
  SqliteUnknownOrderAttestation,
} from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { BotEndRefusalComponent } from '../../bot-end/bot-end-refusal.component';
import { TypedHaltConfirmComponent } from '../../shared/typed-halt-confirm/typed-halt-confirm.component';
import type { PanelAction } from '../lib/broker-v2-panel.types';
import { deriveActionRejection, type ActionRejection } from '../lib/panel-action-outcome';

/** What the operator has said so far: which of the backend's choices, and the Alpaca order id typed for it. */
interface SettleAnswer {
  readonly attestation: '' | SqliteSettlementChoice['attestation'];
  readonly orderId: string;
}

const NOTHING_SAID: SettleAnswer = { attestation: '', orderId: '' };

/** What the Clerk answered about one attestation: what it read, or why it refused. */
interface Answered<T> {
  readonly attestation: SqliteUnknownOrderAttestation;
  readonly value: T;
}

/** What the ticket reads of the Clerk's check: what it found for the attestation the check carried. */
export type SettlementCheck = Pick<SqliteRecoveryActionCheck, 'attestation_findings'>;

/** Said only when a request came back with no words of the backend's own. */
const CHECK_UNANSWERED = 'The check did not finish. Nothing was changed.';
const SETTLE_UNANSWERED = 'No answer came back, so the result is not known. Settle again to see it.';

/** The word the operator types to settle: the order's outcome is decided here, and it cannot be undone. */
const CONFIRM_TOKEN = 'SETTLE';

let nextTicketId = 0;

function sameAttestation(left: SqliteUnknownOrderAttestation, right: SqliteUnknownOrderAttestation): boolean {
  return left.kind === right.kind && (left.broker_order_id ?? null) === (right.broker_order_id ?? null);
}

/** The data plane's own sentence for a field it could not read -- the first message of a 422 -- or `null`. */
function unreadableFieldMessage(error: unknown): string | null {
  if (!(error instanceof HttpErrorResponse) || error.status !== 422) return null;
  const body: unknown = error.error;
  const detail: unknown = typeof body === 'object' && body !== null && 'detail' in body ? body.detail : null;
  if (!Array.isArray(detail)) return null;
  const fields: readonly unknown[] = detail;
  for (const item of fields) {
    if (typeof item === 'object' && item !== null && 'msg' in item && typeof item.msg === 'string') return item.msg;
  }
  return null;
}

/** A refused check or settlement in the backend's words; `fallback` only when it sent none. */
function refusalOf(error: unknown, fallback: string): ActionRejection {
  const unreadable = unreadableFieldMessage(error);
  if (unreadable !== null) {
    return { outcome: 'failure', message: unreadable, why: null, reasonCode: null, nextAction: null };
  }
  return deriveActionRejection(error, fallback);
}

/**
 * The ticket that settles a bot's uncertain closing order (#2854).
 *
 * The Clerk sent a closing order and could not confirm it. The operator looks
 * at the account at Alpaca and says what it shows, from the choices the
 * backend offers: Alpaca has no such order, or it shows it under an Alpaca
 * order id. Nothing is taken on their word. The page first asks the Clerk to
 * check -- it reads Alpaca itself and says what it found -- and only an
 * answer that would settle the order can be confirmed, by typing the word.
 * The Clerk reads Alpaca again when it settles.
 *
 * Every sentence is the backend's: the ticket, each choice, what the Clerk
 * found, the confirmation and a refusal. This component adds the names of
 * its controls. What the Clerk found, and a refusal, belong to what was said
 * when they came back: change the choice or the id and they are gone, so the
 * operator can go back and say the other thing.
 *
 * `check` and `settle` are the page's own requests, as the end card's `save`
 * is: fenced to the lane the ticket was opened on, one at a time. `settle`
 * resolves once the order is settled, and the host then closes the ticket; a
 * refusal leaves it open with the backend's words and changes nothing. The
 * Alpaca order id is trimmed and sent as typed: the backend says whether it
 * is one.
 */
@Component({
  selector: 'app-settle-order-ticket',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [BotEndRefusalComponent, FormField, TimestampDisplayComponent, TypedHaltConfirmComponent],
  templateUrl: './settle-order-ticket.component.html',
  styleUrl: './settle-order-ticket.component.scss',
})
export class SettleOrderTicketComponent {
  /** The settle action as the page presented it when the ticket opened: its order, its choices, its confirmation. */
  readonly action = input.required<PanelAction>();
  /** The account the order is on, shown by its number in the confirmation. */
  readonly accountId = input<string | null>(null);
  /** Asks the Clerk what it reads at Alpaca for what the operator said. Settles nothing; rejects with a refusal. */
  readonly check = input.required<(attestation: SqliteUnknownOrderAttestation) => Promise<SettlementCheck>>();
  /** Settles the order on what the operator said; resolves once it is recorded, rejects with the refusal. */
  readonly settle = input.required<(attestation: SqliteUnknownOrderAttestation) => Promise<void>>();
  /** Another command on this bot is on its way. */
  readonly locked = input(false);

  readonly closed = output();

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly id = nextTicketId++;
  protected readonly titleId = `settle-ticket-${this.id}-title`;
  protected readonly foundId = `settle-ticket-${this.id}-found`;
  protected readonly confirmToken = CONFIRM_TOKEN;

  protected readonly settlement = computed(() => this.action().settlement ?? null);
  protected readonly confirmation = computed(() => this.action().confirmation ?? null);

  protected readonly checking = signal(false);
  protected readonly settling = signal(false);
  protected readonly confirming = signal(false);
  protected readonly busy = computed(() => this.checking() || this.settling() || this.locked());

  /** A ticket opened again is about the order as it stands now: nothing said before carries over. */
  protected readonly answer = linkedSignal({ source: this.action, computation: (): SettleAnswer => NOTHING_SAID });

  private readonly chosen = computed(
    () => this.settlement()?.choices.find((choice) => choice.attestation === this.answer().attestation) ?? null,
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

  /** Moves the keyboard to the ticket, e.g. when the toolbar opens it. */
  focus(): void {
    this.host.nativeElement.querySelector<HTMLElement>('section')?.focus();
  }

  protected async runCheck(): Promise<void> {
    const attestation = this.attestation();
    if (attestation === null || this.busy()) return;
    this.checking.set(true);
    this.lastFound.set(null);
    this.lastRefusal.set(null);
    try {
      const findings = (await this.check()(attestation)).attestation_findings ?? null;
      if (findings === null) {
        this.lastRefusal.set({ attestation, value: refusalOf(null, CHECK_UNANSWERED) });
      } else {
        this.lastFound.set({ attestation, value: findings });
      }
    } catch (error) {
      this.lastRefusal.set({ attestation, value: refusalOf(error, CHECK_UNANSWERED) });
    } finally {
      this.checking.set(false);
    }
  }

  /** Settle on exactly what the Clerk checked; nothing while what it found would not settle the order. */
  protected async runSettle(): Promise<void> {
    this.confirming.set(false);
    const attestation = this.attestation();
    if (attestation === null || this.busy() || this.found()?.would_settle !== true) return;
    this.settling.set(true);
    this.lastRefusal.set(null);
    try {
      await this.settle()(attestation);
    } catch (error) {
      this.lastRefusal.set({ attestation, value: refusalOf(error, SETTLE_UNANSWERED) });
    } finally {
      this.settling.set(false);
    }
  }

  private current<T>(answered: Answered<T> | null): T | null {
    const attestation = this.attestation();
    return answered !== null && attestation !== null && sameAttestation(answered.attestation, attestation)
      ? answered.value
      : null;
  }
}
