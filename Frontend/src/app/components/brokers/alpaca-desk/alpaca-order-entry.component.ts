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
  ManualOrderCapability,
  ManualOrderPreview,
  ManualOrderTicket,
  OrderLegResult,
} from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
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
  private readonly manualPreview = signal<ManualOrderPreview | null>(null);
  protected readonly submitError = signal<string | null>(null);
  /** Fires after any broker submission attempt, including uncertain outcomes. */
  readonly submissionFinished = output();

  private nextId = 1;

  protected readonly canSubmit = computed(
    () =>
      this.expectedAccountId().trim().length > 0
      && this.legs().length > 0
      && this.legs().every((leg) => this.legValid(leg)),
  );

  protected readonly manualMarketOnly = computed(() => this.sqliteManualAuthority());
  protected readonly manualSubmissionAvailable = computed(
    () => !this.sqliteManualAuthority() || this.manualCapability()?.available === true,
  );
  protected readonly canConfirm = computed(
    () =>
      this.canSubmit()
      && this.manualSubmissionAvailable()
      && (!this.sqliteManualAuthority()
        || (this.manualTicketId() !== null && this.manualLegId() !== null)),
  );

  constructor() {
    effect(() => {
      if (!this.sqliteManualAuthority()) return;
      const ticketId = this.manualTicketId();
      if (ticketId === null) return;
      void this.restoreManualTicket(ticketId);
    });
    effect((onCleanup) => {
      const ticket = this.manualTicket();
      if (!this.sqliteManualAuthority() || ticket === null || !this.ticketNeedsRefresh(ticket)) return;
      const refresh = globalThis.setInterval(() => void this.restoreManualTicket(ticket.ticket_id), 5_000);
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
    if (this.sqliteManualAuthority()) return;
    this.legs.update((legs) => [...legs, this.newLeg(this.nextId++, '')]);
    // A new draft invalidates the last submit's results view.
    this.results.set(null);
    this.submitError.set(null);
  }

  protected removeLeg(id: number): void {
    if (this.sqliteManualAuthority()) return;
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
    const legId = this.manualLegId();
    const leg = this.legs()[0];
    if (ticketId === null || legId === null || leg === undefined) return;
    this.submitting.set(true);
    try {
      const preview = await this.brokers.previewSqliteManualOrder(this.expectedAccountId(), {
        ticket_id: ticketId,
        leg: { leg_id: legId, instruction: this.toRequestLeg(leg) },
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
    const legId = this.manualLegId();
    const leg = this.legs()[0];
    const previewToken = this.manualPreview()?.preview_token;
    if (ticketId === null || legId === null || leg === undefined || previewToken === null || previewToken === undefined) {
      throw new Error('Refresh the manual order preview before confirming.');
    }
    const ticket = await this.brokers.submitSqliteManualOrder(this.expectedAccountId(), ticketId, {
      leg: { leg_id: legId, instruction: this.toRequestLeg(leg) },
      preview_token: previewToken,
    });
    this.manualTicket.set(ticket);
  }

  private async restoreManualTicket(ticketId: string): Promise<void> {
    try {
      const ticket = await this.brokers.getSqliteManualOrderTicket(
        this.expectedAccountId(),
        ticketId,
      );
      this.manualTicket.set(ticket);
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 404) return;
      this.submitError.set(this.submissionErrorMessage(err));
    }
  }

  private ticketNeedsRefresh(ticket: ManualOrderTicket): boolean {
    return !['COMPLETED', 'CANCELED'].includes(ticket.state);
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
