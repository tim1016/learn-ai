import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { DialogModule } from 'primeng/dialog';
import { InputNumberModule } from 'primeng/inputnumber';
import { InputTextModule } from 'primeng/inputtext';
import { MessageModule } from 'primeng/message';
import { SelectButtonModule } from 'primeng/selectbutton';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';

import type {
  BrokerOrderLeg,
  OrderLegResult,
  OrderSide,
  OrderType,
  TimeInForce,
} from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokersService } from '../../../services/brokers.service';

/** A draft equity leg the operator is assembling (pre-submit). */
interface DraftLeg {
  readonly id: number;
  symbol: string;
  side: OrderSide;
  quantity: number | null;
  orderType: OrderType;
  // Only meaningful (and required) when orderType is 'limit'.
  limitPrice: number | null;
  timeInForce: TimeInForce;
}

interface SideOption {
  readonly label: string;
  readonly value: OrderSide;
}

interface OrderTypeOption {
  readonly label: string;
  readonly value: OrderType;
}

interface TifOption {
  readonly label: string;
  readonly value: TimeInForce;
}

const SIDE_OPTIONS: SideOption[] = [
  { label: 'Buy', value: 'buy' },
  { label: 'Sell', value: 'sell' },
];

const ORDER_TYPE_OPTIONS: OrderTypeOption[] = [
  { label: 'Market', value: 'market' },
  { label: 'Limit', value: 'limit' },
];

const TIF_OPTIONS: TifOption[] = [
  { label: 'Day', value: 'day' },
  { label: 'GTC', value: 'gtc' },
];

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
    FormsModule,
    ButtonModule,
    DialogModule,
    InputNumberModule,
    InputTextModule,
    MessageModule,
    SelectButtonModule,
    TableModule,
    TagModule,
    ReceiptLabelPipe,
  ],
  templateUrl: './alpaca-order-entry.component.html',
  host: { class: 'block' },
})
export class AlpacaOrderEntryComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly sideOptions: SideOption[] = SIDE_OPTIONS;
  protected readonly orderTypeOptions: OrderTypeOption[] = ORDER_TYPE_OPTIONS;
  protected readonly tifOptions: TifOption[] = TIF_OPTIONS;
  // S1 has no operator-identity plumbing yet; the manual namespace uses a fixed
  // desk operator. Later slices thread the signed-in operator through here.
  private readonly operator = 'desk';

  protected readonly legs = signal<DraftLeg[]>([]);
  protected readonly previewOpen = signal(false);
  protected readonly submitting = signal(false);
  protected readonly results = signal<OrderLegResult[] | null>(null);
  protected readonly submitError = signal<string | null>(null);

  private nextId = 0;

  protected readonly canSubmit = computed(
    () => this.legs().length > 0 && this.legs().every((leg) => this.legValid(leg)),
  );

  protected legValid(leg: DraftLeg): boolean {
    const baseValid =
      leg.symbol.trim().length > 0 && leg.quantity != null && leg.quantity > 0;
    if (leg.orderType === 'limit') {
      // A limit order rests at a chosen price — it must be present and positive.
      return baseValid && leg.limitPrice != null && leg.limitPrice > 0;
    }
    return baseValid;
  }

  protected addEquityLeg(): void {
    this.legs.update((legs) => [
      ...legs,
      {
        id: this.nextId++,
        symbol: '',
        side: 'buy',
        quantity: null,
        orderType: 'market',
        limitPrice: null,
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

  protected updateSymbol(id: number, symbol: string): void {
    this.legs.update((legs) =>
      legs.map((leg) => (leg.id === id ? { ...leg, symbol: symbol.toUpperCase() } : leg)),
    );
  }

  protected updateSide(id: number, side: OrderSide): void {
    this.legs.update((legs) => legs.map((leg) => (leg.id === id ? { ...leg, side } : leg)));
  }

  protected updateQuantity(id: number, quantity: number | null): void {
    this.legs.update((legs) =>
      legs.map((leg) => (leg.id === id ? { ...leg, quantity } : leg)),
    );
  }

  protected updateOrderType(id: number, orderType: OrderType): void {
    this.legs.update((legs) =>
      legs.map((leg) =>
        leg.id === id
          ? // Switching back to market clears the now-meaningless limit price so
            // a stale value never rides along on the submit payload.
            { ...leg, orderType, limitPrice: orderType === 'limit' ? leg.limitPrice : null }
          : leg,
      ),
    );
  }

  protected updateLimitPrice(id: number, limitPrice: number | null): void {
    this.legs.update((legs) =>
      legs.map((leg) => (leg.id === id ? { ...leg, limitPrice } : leg)),
    );
  }

  protected updateTimeInForce(id: number, timeInForce: TimeInForce): void {
    this.legs.update((legs) =>
      legs.map((leg) => (leg.id === id ? { ...leg, timeInForce } : leg)),
    );
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
      legs: this.legs().map((leg): BrokerOrderLeg => this.toRequestLeg(leg)),
    };
    try {
      const result = await this.brokers.submitOrder('alpaca', request);
      this.results.set(result.results);
      this.previewOpen.set(false);
      this.legs.set([]);
    } catch {
      this.submitError.set('Could not reach Alpaca to submit the order. Nothing was sent.');
    } finally {
      this.submitting.set(false);
    }
  }

  /**
   * Shape one draft leg into the wire contract. A limit leg carries its price;
   * a market leg omits it entirely (the backend validator forbids a price on a
   * market order), so `limit_price` is only set on the limit branch.
   */
  private toRequestLeg(leg: DraftLeg): BrokerOrderLeg {
    const base: BrokerOrderLeg = {
      symbol: leg.symbol.trim(),
      side: leg.side,
      quantity: leg.quantity as number,
      order_type: leg.orderType,
      time_in_force: leg.timeInForce,
    };
    return leg.orderType === 'limit'
      ? { ...base, limit_price: leg.limitPrice as number }
      : base;
  }

  protected trackLeg = (_: number, leg: DraftLeg): number => leg.id;
  protected trackResult = (_: number, result: OrderLegResult): string => result.order_ref;
}
