import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from '@angular/core';
import { form } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';

import type {
  BrokerOrderLeg,
  ManualOrderCancellation,
  ManualOrderCapability,
  ManualOrderPreview,
  ManualOrderTicket,
  OrderLegResult,
} from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { AlpacaOrderDraftLeg } from './alpaca-order-entry.types';
import { AlpacaOrderLegRowComponent } from './alpaca-order-leg-row.component';
import { AlpacaOrderPreviewComponent } from './alpaca-order-preview.component';
import { AlpacaOrderResultsComponent } from './alpaca-order-results.component';

/**
 * Alpaca order-entry panel (phase-2). Leg-based paradigm: the operator adds
 * equity legs, previews, confirms, and submits. S2 adds a per-leg order-type
 * selector (Market | Limit) — a limit leg reveals a limit-price input and rests
 * as a working order — plus a time-in-force selector (Day | GTC). Option legs
 * are present but disabled ("coming in 2b"). Per-leg results render after
 * submit — acked or a typed failure.
 */
@Component({
  selector: 'app-alpaca-order-entry',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ButtonModule,
    MessageModule,
    AlpacaOrderLegRowComponent,
    AlpacaOrderPreviewComponent,
    AlpacaOrderResultsComponent,
    AssetIdentityComponent,
    ReceiptLabelPipe,
    TimestampDisplayComponent,
  ],
  templateUrl: './alpaca-order-entry.component.html',
  styleUrl: './alpaca-order-entry.component.scss',
  host: { class: 'block' },
})
export class AlpacaOrderEntryComponent {
  private readonly brokers = inject(BrokersService);
  readonly initialSymbol = input('');
  readonly expectedAccountId = input.required<string>();
  /** True only when the active Account Clerk is SQLite-backed. */
  readonly sqliteManualAuthority = input(false);
  readonly manualTicketId = input<string | null>(null);
  readonly manualLegId = input<string | null>(null);
  readonly manualCapability = input<ManualOrderCapability | null>(null);

  // S1 has no operator-identity plumbing yet; the manual namespace uses a fixed
  // desk operator. Later slices thread the signed-in operator through here.
  private readonly operator = 'desk';

  protected readonly legs = linkedSignal<AlpacaOrderDraftLeg[]>(() => [
    this.newLeg(0, this.initialSymbol()),
  ]);
  protected readonly legsForm = form(this.legs);
  protected readonly previewOpen = signal(false);
  protected readonly submitting = signal(false);
  protected readonly results = signal<OrderLegResult[] | null>(null);
  protected readonly manualTicket = signal<ManualOrderTicket | null>(null);
  protected readonly manualCancellation = signal<ManualOrderCancellation | null>(null);
  private readonly manualPreview = signal<ManualOrderPreview | null>(null);
  private readonly manualLegIds = signal<ReadonlyMap<number, string>>(new Map());
  private readonly manualCancelRequestId = signal<string | null>(null);
  private readonly manualCancelTicketId = signal<string | null>(null);
  protected readonly submitError = signal<string | null>(null);
  protected readonly cancelling = signal(false);
  /** Fires after any broker submission attempt, including uncertain outcomes. */
  readonly submissionFinished = output();

  private nextId = 1;

  protected readonly canSubmit = computed(
    () =>
      this.expectedAccountId().trim().length > 0
      && this.legs().length > 0
      && this.legs().every((leg) => this.legValid(leg)),
  );

  protected readonly manualTicketMode = computed(() => this.sqliteManualAuthority());
  protected readonly manualSubmissionAvailable = computed(
    () =>
      !this.sqliteManualAuthority()
      || this.manualCapability()?.available === true
      || this.legs().every((leg) => leg.side === 'sell'),
  );
  protected readonly activeManualLeg = computed(() =>
    this.manualTicket()?.legs.find((leg) => leg.order !== null && !['SUCCEEDED', 'FAILED', 'CANCELED'].includes(leg.state)) ?? null,
  );
  protected readonly displayedManualCancellation = computed(
    () => this.activeManualLeg()?.cancellation ?? this.manualCancellation(),
  );
  protected readonly cancelableManualTicketId = computed(() => {
    const ticket = this.manualTicket();
    return ticket !== null
      && this.ticketNeedsRefresh(ticket)
      && ticket.legs.some(
        (leg) => leg.order !== null
          && ['ACCEPTED', 'IN_PROGRESS'].includes(leg.state)
          && leg.cancellation === null,
      )
      ? ticket.ticket_id
      : null;
  });
  protected readonly canConfirm = computed(
    () =>
      this.canSubmit()
      && this.manualSubmissionAvailable()
      && (!this.sqliteManualAuthority() || this.manualTicketId() !== null),
  );
  protected readonly canContinueTicket = computed(() => {
    const ticket = this.manualTicket();
    if (ticket === null || ticket.state === 'PAUSED_UNKNOWN' || ticket.state === 'CANCELED') return false;
    const nextIndex = ticket.legs.findIndex((leg) => leg.state === 'RESERVED');
    return nextIndex > 0 && ticket.legs.slice(0, nextIndex).every((leg) => leg.state !== 'ACCEPTED');
  });

  constructor() {
    effect(() => {
      // A cancellation id is immutable for one target order only. Route/input
      // reuse must never carry it to the next manual ticket or leg.
      this.manualTicketId();
      this.manualLegId();
      this.manualCancelRequestId.set(null);
      this.manualCancelTicketId.set(null);
      this.manualCancellation.set(null);
      this.manualLegIds.set(new Map());
    });
    effect(() => {
      const accountId = this.expectedAccountId();
      if (!this.sqliteManualAuthority()) return;
      const ticketId = this.manualTicketId();
      this.manualTicket.set(null);
      if (ticketId === null) return;
      void this.restoreManualTicket(ticketId, accountId);
    });
    effect((onCleanup) => {
      const ticket = this.manualTicket();
      if (!this.sqliteManualAuthority() || ticket === null || !this.ticketNeedsRefresh(ticket)) return;
      const refresh = globalThis.setInterval(
        () => void this.restoreManualTicket(ticket.ticket_id, this.expectedAccountId()),
        5_000,
      );
      onCleanup(() => globalThis.clearInterval(refresh));
    });
  }

  protected legValid(leg: AlpacaOrderDraftLeg): boolean {
    const quantity = Number(leg.quantity);
    const baseValid =
      leg.symbol.trim().length > 0 &&
      leg.quantity.trim().length > 0 &&
      Number.isFinite(quantity) &&
      quantity > 0;
    if (leg.orderType !== 'limit') return baseValid;

    const limitPrice = Number(leg.limitPrice);
    return (
      baseValid &&
      leg.limitPrice.trim().length > 0 &&
      Number.isFinite(limitPrice) &&
      limitPrice > 0
    );
  }

  protected addEquityLeg(): void {
    this.legs.update((legs) => [...legs, this.newLeg(this.nextId++, '')]);
    // A new draft invalidates the last submit's results view.
    this.results.set(null);
    this.submitError.set(null);
  }

  protected removeLeg(id: number): void {
    this.legs.update((legs) => legs.filter((leg) => leg.id !== id));
    // Editing the draft (removing a leg) invalidates the last submit's results
    // view, so a stale results table isn't left rendered against an empty draft.
    this.results.set(null);
    this.submitError.set(null);
  }

  protected async openPreview(): Promise<void> {
    if (!this.canConfirm() || this.submitting()) return;
    this.submitError.set(null);
    if (!this.sqliteManualAuthority()) {
      this.previewOpen.set(true);
      return;
    }
    const ticketId = this.manualTicketId();
    if (ticketId === null) return;
    this.submitting.set(true);
    try {
      const preview = await this.brokers.previewSqliteManualOrder(this.expectedAccountId(), {
        ticket_id: ticketId,
        legs: this.manualRequestLegs(),
      });
      this.manualPreview.set(preview);
      if (!preview.capability.available || preview.preview_token === null) {
        this.submitError.set(preview.capability.unavailable?.message ?? 'Manual order is unavailable.');
        return;
      }
      this.previewOpen.set(true);
    } catch (err) {
      this.submitError.set(this.submissionErrorMessage(err));
    } finally {
      this.submitting.set(false);
    }
  }

  protected closePreview(): void {
    this.previewOpen.set(false);
  }

  protected async confirmSubmit(): Promise<void> {
    if (!this.canConfirm() || this.submitting()) return;
    this.submitting.set(true);
    this.submitError.set(null);
    try {
      if (this.sqliteManualAuthority()) {
        await this.confirmSqliteManualOrder();
      } else {
        const request = {
          operator: this.operator,
          expected_account_id: this.expectedAccountId(),
          legs: this.legs().map((leg) => this.toRequestLeg(leg)),
        };
        const result = await this.brokers.submitOrder('alpaca', request);
        this.results.set(result.results);
        this.legs.set([]);
      }
      this.previewOpen.set(false);
    } catch (err) {
      this.submitError.set(this.submissionErrorMessage(err));
      this.previewOpen.set(false);
    } finally {
      this.submitting.set(false);
      this.submissionFinished.emit();
    }
  }

  private async confirmSqliteManualOrder(): Promise<void> {
    const ticketId = this.manualTicketId();
    const previewToken = this.manualPreview()?.preview_token;
    if (ticketId === null || previewToken === null || previewToken === undefined) {
      throw new Error('Refresh the manual order preview before confirming.');
    }
    const ticket = await this.brokers.submitSqliteManualOrder(this.expectedAccountId(), ticketId, {
      legs: this.manualRequestLegs(),
      preview_token: previewToken,
    });
    this.manualTicket.set(ticket);
  }

  private async restoreManualTicket(ticketId: string, accountId: string): Promise<void> {
    try {
      const ticket = await this.brokers.getSqliteManualOrderTicket(
        accountId,
        ticketId,
      );
      if (this.manualTicketId() !== ticketId || this.expectedAccountId() !== accountId) return;
      this.manualTicket.set(ticket);
      const cancellation = ticket.legs.find((leg) => leg.cancellation !== null)?.cancellation ?? null;
      if (this.manualCancelTicketId() !== null && this.manualCancelTicketId() !== ticket.ticket_id) {
        this.manualCancelRequestId.set(null);
        this.manualCancelTicketId.set(null);
      }
      this.manualCancellation.set(cancellation);
    } catch (err) {
      if (
        (err instanceof HttpErrorResponse && err.status === 404)
        || this.manualTicketId() !== ticketId
        || this.expectedAccountId() !== accountId
      ) return;
      this.submitError.set(this.submissionErrorMessage(err));
    }
  }

  private ticketNeedsRefresh(ticket: ManualOrderTicket): boolean {
    return !['COMPLETED', 'CANCELED', 'PAUSED_UNKNOWN'].includes(ticket.state);
  }

  protected async cancelManualTicket(): Promise<void> {
    const ticketId = this.cancelableManualTicketId();
    if (ticketId === null || this.cancelling()) return;
    this.cancelling.set(true);
    this.submitError.set(null);
    try {
      const cancelRequestId =
        this.manualCancelTicketId() === ticketId
          ? (this.manualCancelRequestId() ?? this.newStableRequestId())
          : this.newStableRequestId();
      this.manualCancelRequestId.set(cancelRequestId);
      this.manualCancelTicketId.set(ticketId);
      const ticket = await this.brokers.cancelSqliteManualOrderTicket(this.expectedAccountId(), ticketId, {
        cancel_request_id: cancelRequestId,
      });
      this.manualTicket.set(ticket);
      this.manualCancellation.set(
        ticket.legs.find((leg) => leg.cancellation !== null)?.cancellation ?? null,
      );
    } catch (err) {
      this.submitError.set(this.submissionErrorMessage(err));
    } finally {
      this.cancelling.set(false);
      this.submissionFinished.emit();
    }
  }

  protected async continueManualTicket(): Promise<void> {
    const ticket = this.manualTicket();
    if (ticket === null || !this.canContinueTicket() || this.submitting()) return;
    const legs = ticket.legs.map((leg) => {
      if (leg.instruction === null) {
        throw new Error('The Clerk cannot recover this ticket leg for a safe continuation.');
      }
      return { leg_id: leg.leg_id, instruction: leg.instruction };
    });
    this.submitting.set(true);
    this.submitError.set(null);
    try {
      const preview = await this.brokers.previewSqliteManualOrder(this.expectedAccountId(), {
        ticket_id: ticket.ticket_id,
        legs,
      });
      if (!preview.capability.available || preview.preview_token === null) {
        this.submitError.set(preview.capability.unavailable?.message ?? 'Manual ticket continuation is unavailable.');
        return;
      }
      this.manualTicket.set(await this.brokers.continueSqliteManualOrderTicket(
        this.expectedAccountId(),
        ticket.ticket_id,
        { legs, preview_token: preview.preview_token },
      ));
    } catch (err) {
      this.submitError.set(this.submissionErrorMessage(err));
    } finally {
      this.submitting.set(false);
      this.submissionFinished.emit();
    }
  }

  private newStableRequestId(): string {
    if (typeof globalThis.crypto?.randomUUID !== 'function') {
      throw new Error('This browser cannot create a durable request identity.');
    }
    return globalThis.crypto.randomUUID();
  }

  private toRequestLeg(leg: AlpacaOrderDraftLeg): BrokerOrderLeg {
    const base: BrokerOrderLeg = {
      symbol: leg.symbol.trim().toUpperCase(),
      side: leg.side,
      quantity: Number(leg.quantity),
      order_type: leg.orderType,
      time_in_force: leg.timeInForce,
    };
    return leg.orderType === 'limit'
      ? { ...base, limit_price: Number(leg.limitPrice) }
      : base;
  }

  private manualRequestLegs(): { leg_id: string; instruction: BrokerOrderLeg }[] {
    const ids = new Map(this.manualLegIds());
    let changed = false;
    const legs = this.legs().map((leg, index) => {
      let legId = ids.get(leg.id);
      if (legId === undefined) {
        legId = index === 0 && this.manualLegId() !== null
          ? this.manualLegId()!
          : this.newStableRequestId();
        ids.set(leg.id, legId);
        changed = true;
      }
      return { leg_id: legId, instruction: this.toRequestLeg(leg) };
    });
    if (changed) this.manualLegIds.set(ids);
    return legs;
  }

  private newLeg(id: number, symbol: string): AlpacaOrderDraftLeg {
    return {
      id,
      symbol: symbol.trim().toUpperCase(),
      side: 'buy',
      quantity: '',
      orderType: 'market',
      limitPrice: '',
      timeInForce: 'day',
    };
  }

  private submissionErrorMessage(err: unknown): string {
    if (err instanceof Error && err.message === 'Refresh the manual order preview before confirming.') {
      return err.message;
    }
    if (err instanceof HttpErrorResponse && err.status !== 0) {
      const detail = err.error?.detail;
      const nestedMessage =
        detail && typeof detail === 'object' && 'message' in detail
          ? detail.message
          : undefined;
      const message =
        typeof detail === 'string'
          ? detail
          : typeof nestedMessage === 'string'
            ? nestedMessage
            : err.statusText || `HTTP ${err.status}`;
      return `Order rejected: ${message}`;
    }
    return 'The submission outcome is uncertain. Check Alpaca orders and the journal before submitting again.';
  }
}
