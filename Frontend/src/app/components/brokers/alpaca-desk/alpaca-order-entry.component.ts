import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { form } from '@angular/forms/signals';
import { ButtonModule } from 'primeng/button';
import { MessageModule } from 'primeng/message';
import { TooltipModule } from 'primeng/tooltip';

import type { BrokerOrderLeg, OrderLegResult } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
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
    TooltipModule,
    AlpacaOrderLegRowComponent,
    AlpacaOrderPreviewComponent,
    AlpacaOrderResultsComponent,
  ],
  templateUrl: './alpaca-order-entry.component.html',
  host: { class: 'block' },
})
export class AlpacaOrderEntryComponent {
  private readonly brokers = inject(BrokersService);

  // S1 has no operator-identity plumbing yet; the manual namespace uses a fixed
  // desk operator. Later slices thread the signed-in operator through here.
  private readonly operator = 'desk';

  protected readonly legs = signal<AlpacaOrderDraftLeg[]>([]);
  protected readonly legsForm = form(this.legs);
  protected readonly previewOpen = signal(false);
  protected readonly submitting = signal(false);
  protected readonly results = signal<OrderLegResult[] | null>(null);
  protected readonly submitError = signal<string | null>(null);

  private nextId = 0;

  protected readonly canSubmit = computed(
    () => this.legs().length > 0 && this.legs().every((leg) => this.legValid(leg)),
  );

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
    this.legs.update((legs) => [
      ...legs,
      {
        id: this.nextId++,
        symbol: '',
        side: 'buy',
        quantity: '',
        orderType: 'market',
        limitPrice: '',
        timeInForce: 'day',
      },
    ]);
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

  protected openPreview(): void {
    if (!this.canSubmit()) return;
    this.previewOpen.set(true);
  }

  protected closePreview(): void {
    this.previewOpen.set(false);
  }

  protected async confirmSubmit(): Promise<void> {
    if (!this.canSubmit() || this.submitting()) return;
    this.submitting.set(true);
    this.submitError.set(null);
    const request = {
      operator: this.operator,
      legs: this.legs().map((leg) => this.toRequestLeg(leg)),
    };
    try {
      const result = await this.brokers.submitOrder('alpaca', request);
      this.results.set(result.results);
      this.previewOpen.set(false);
      this.legs.set([]);
    } catch (err) {
      this.submitError.set(this.submissionErrorMessage(err));
      this.previewOpen.set(false);
    } finally {
      this.submitting.set(false);
    }
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
