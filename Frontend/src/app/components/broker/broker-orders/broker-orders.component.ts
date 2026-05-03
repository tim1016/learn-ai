import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import type {
  IbkrOpenOrder,
  IbkrOrderAck,
  IbkrOrderEvent,
  IbkrOrderSpec,
  OrderAction,
  OrderTimeInForce,
  OrderType,
  SecType,
} from '../../../api/broker-models';
import { fmtCurrency, fmtSignedNumber, fmtTimestampNy } from '../format';

const ORDER_EVENT_BUFFER = 50;

interface OrderEventLine extends IbkrOrderEvent {
  /** Pre-rendered ET timestamp string for the log row. */
  displayTs: string;
}

/**
 * /broker/orders — paper order placement, list, cancel, event stream.
 *
 * Mirrors the four-layer backend safety in the form UX:
 *   1. ``IBKR_MODE`` env var is the server's first gate.
 *   2. Port-vs-mode validator runs server-side too.
 *   3. DU account-id sentinel — surfaced here as ``isPaperConnected``;
 *      the form is locked when this is false even if the user can see
 *      a ``connected`` banner.
 *   4. ``confirm_paper`` checkbox — required-true on every submit.
 *
 * The submit handler also generates a fresh ``client_order_id`` UUID
 * so the request is idempotent on retry.
 */
@Component({
  selector: 'app-broker-orders',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, PageHeaderComponent],
  styleUrl: './broker-orders.component.scss',
  templateUrl: './broker-orders.component.html',
})
export class BrokerOrdersComponent {
  private readonly broker = inject(BrokerService);
  private readonly health = inject(BrokerHealthService);
  private readonly injector = inject(Injector);

  // Form state
  readonly symbol = signal('SPY');
  readonly secType = signal<SecType>('STK');
  readonly action = signal<OrderAction>('BUY');
  readonly quantity = signal(1);
  readonly orderType = signal<OrderType>('MKT');
  readonly limitPrice = signal<number | null>(null);
  readonly tif = signal<OrderTimeInForce>('DAY');
  readonly confirmPaper = signal(false);
  readonly expiryMs = signal<number | null>(null);
  readonly strike = signal<number | null>(null);
  readonly right = signal<'C' | 'P'>('C');

  readonly submitting = signal(false);
  readonly placeError = signal<string | null>(null);
  readonly lastAck = signal<IbkrOrderAck | null>(null);

  // Open orders list
  readonly openOrdersLoading = signal(false);
  readonly openOrdersError = signal<string | null>(null);
  readonly openOrders = signal<IbkrOpenOrder[]>([]);

  // Order events SSE
  private readonly eventStream = signal<SseStream<IbkrOrderEvent> | null>(null);
  readonly eventStatus = computed(() => this.eventStream()?.status() ?? 'idle');
  readonly eventStreamError = computed(() => this.eventStream()?.lastError() ?? null);
  readonly eventLines = computed<OrderEventLine[]>(() => {
    const stream = this.eventStream();
    if (stream === null) return [];
    return stream.data().map((ev) => ({
      ...ev,
      displayTs: fmtTimestampNy(ev.ts_ms),
    }));
  });

  readonly isPaperConnected = this.health.isPaperConnected;
  readonly canSubmit = computed(
    () => this.isPaperConnected() && this.confirmPaper() && !this.submitting(),
  );

  /** Lock indicator for the "live trading not enabled" banner. */
  readonly liveModeLocked = computed(() => {
    const h = this.health.health();
    return h !== null && h.connected === true && h.is_paper !== true;
  });

  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedNumber = fmtSignedNumber;
  readonly fmtTimestampNy = fmtTimestampNy;

  constructor() {
    void this.refreshOpenOrders();
    this.openEventStream();

    // Refresh open orders whenever a new event arrives — the event
    // log itself isn't authoritative, but the open-orders endpoint is.
    effect(() => {
      const stream = this.eventStream();
      if (stream === null) return;
      const data = stream.data();
      if (data.length > 0) {
        void this.refreshOpenOrders();
      }
    });
  }

  async refreshOpenOrders(): Promise<void> {
    if (!this.health.health()?.connected) return;
    this.openOrdersLoading.set(true);
    this.openOrdersError.set(null);
    try {
      const orders = await this.broker.openOrders();
      this.openOrders.set(orders);
    } catch (err) {
      this.openOrdersError.set(extractMessage(err));
    } finally {
      this.openOrdersLoading.set(false);
    }
  }

  async submitOrder(): Promise<void> {
    if (!this.canSubmit()) return;

    this.submitting.set(true);
    this.placeError.set(null);

    const spec: IbkrOrderSpec = {
      symbol: this.symbol().toUpperCase(),
      sec_type: this.secType(),
      action: this.action(),
      quantity: this.quantity(),
      order_type: this.orderType(),
      limit_price: this.orderType() === 'LMT' ? this.limitPrice() : null,
      time_in_force: this.tif(),
      confirm_paper: this.confirmPaper(),
      client_order_id: cryptoUuid(),
      multiplier: 100,
      expiry_ms: this.secType() === 'OPT' ? this.expiryMs() : null,
      strike: this.secType() === 'OPT' ? this.strike() : null,
      right: this.secType() === 'OPT' ? this.right() : null,
    };

    try {
      const ack = await this.broker.placeOrder(spec);
      this.lastAck.set(ack);
      // Don't auto-clear the form — the user wants to confirm what
      // they placed. Reset the explicit-confirm checkbox so the next
      // submit requires another deliberate click.
      this.confirmPaper.set(false);
      void this.refreshOpenOrders();
    } catch (err) {
      this.placeError.set(extractMessage(err));
    } finally {
      this.submitting.set(false);
    }
  }

  async cancel(orderId: number): Promise<void> {
    try {
      await this.broker.cancelOrder(orderId);
      void this.refreshOpenOrders();
    } catch (err) {
      this.openOrdersError.set(extractMessage(err));
    }
  }

  trackOrder = (_: number, o: IbkrOpenOrder): number => o.order_id;
  trackEvent = (_: number, e: OrderEventLine): string =>
    `${e.order_id}:${e.ts_ms}:${e.event_type}`;

  fillProgress(o: IbkrOpenOrder): number {
    if (o.quantity === 0) return 0;
    return Math.max(0, Math.min(1, o.cumulative_filled / o.quantity));
  }

  eventColor(e: IbkrOrderEvent): string {
    switch (e.event_type) {
      case 'fill':
        return 'event-fill';
      case 'cancel':
        return 'event-cancel';
      case 'error':
        return 'event-error';
      default:
        return 'event-status';
    }
  }

  private openEventStream(): void {
    const existing = this.eventStream();
    if (existing) existing.close();
    const stream = runInInjectionContext(this.injector, () =>
      brokerSse<IbkrOrderEvent>('/api/broker/orders/stream?poll_ms=500', 'order', {
        maxBuffer: ORDER_EVENT_BUFFER,
      }),
    );
    this.eventStream.set(stream);
  }
}

function cryptoUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Fallback for non-secure contexts. Not cryptographically secure;
  // sufficient as an idempotency token for paper orders only.
  return Date.now().toString(16) + '-' + Math.random().toString(16).slice(2, 10);
}

function extractMessage(err: unknown): string {
  if (err == null) return 'Unknown error';
  if (typeof err === 'object' && err !== null && 'error' in err) {
    const inner = (err as { error?: { detail?: string } }).error;
    if (inner?.detail) return inner.detail;
  }
  if (typeof err === 'object' && err !== null && 'message' in err) {
    return String((err as { message: unknown }).message);
  }
  if (typeof err === 'string') return err;
  return 'Unknown error';
}
