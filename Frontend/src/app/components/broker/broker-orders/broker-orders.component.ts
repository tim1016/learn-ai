import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  computed,
  effect,
  inject,
  runInInjectionContext,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { RouterLink } from '@angular/router';
import { PaperOnlyDirective } from '../../../shared/directives/paper-only.directive';
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
const CONFIRM_DIALOG_COOLDOWN_MS = 3000;
const CONFIRM_DIALOG_TICK_MS = 100;

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
  imports: [FormsModule, PageHeaderComponent, SectionErrorComponent, PaperOnlyDirective, RouterLink],
  styleUrl: './broker-orders.component.scss',
  templateUrl: './broker-orders.component.html',
})
export class BrokerOrdersComponent {
  private readonly broker = inject(BrokerService);
  private readonly health = inject(BrokerHealthService);
  readonly bannerState = this.health.bannerState;
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);

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
  readonly placeError = signal<unknown>(null);
  readonly lastAck = signal<IbkrOrderAck | null>(null);

  // Confirm-paper modal — layer 4 of the safety stack, mirrored from
  // the server-side ``confirm_paper`` requirement. Submit opens the
  // dialog; the user must tick the checkbox AND wait out a 3-second
  // cooldown before the Place button enables. The cooldown is what
  // catches absent-minded muscle-memory clicks where the order summary
  // disagrees with what the user thought they were submitting.
  //
  // Rendered as a native ``<dialog>`` so focus moves into the dialog
  // on showModal(), focus is trapped while open, Escape dispatches a
  // cancel event, and focus is restored to the previously-focused
  // element on close — no hand-rolled focus management.
  readonly confirmDialogOpen = signal(false);
  readonly confirmCheckbox = signal(false);
  readonly confirmCooldownMs = signal(0);
  private confirmTickHandle: ReturnType<typeof setInterval> | null = null;
  private readonly confirmDialogRef = viewChild<ElementRef<HTMLDialogElement>>('confirmDialog');
  readonly confirmCanPlace = computed(
    () =>
      this.confirmDialogOpen() &&
      this.confirmCheckbox() &&
      this.confirmCooldownMs() === 0 &&
      !this.submitting() &&
      this.isPaperConnected(),
  );

  // Open orders list
  readonly openOrdersLoading = signal(false);
  readonly openOrdersError = signal<unknown>(null);
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
  readonly accountId = computed(() => this.health.health()?.account_id ?? null);
  /**
   * Submit-button gate: the form opens the confirm dialog rather than
   * placing immediately, so this only checks that the broker is paper-
   * connected and we aren't already mid-submit.
   */
  readonly canSubmit = computed(
    () => this.isPaperConnected() && !this.submitting(),
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

    this.destroyRef.onDestroy(() => this.clearConfirmTick());

    // Drive native <dialog> open/close from the confirmDialogOpen
    // signal. showModal() gives focus management, focus trap, and
    // Escape-to-cancel for free; close() restores focus.
    effect(() => {
      const open = this.confirmDialogOpen();
      const dialog = this.confirmDialogRef()?.nativeElement;
      if (!dialog) return;
      if (open && !dialog.open) {
        dialog.showModal();
      } else if (!open && dialog.open) {
        dialog.close();
      }
    });
  }

  openConfirmDialog(): void {
    if (!this.canSubmit()) return;
    this.placeError.set(null);
    this.confirmCheckbox.set(false);
    this.confirmCooldownMs.set(CONFIRM_DIALOG_COOLDOWN_MS);
    this.confirmDialogOpen.set(true);
    this.startConfirmTick();
  }

  cancelConfirmDialog(): void {
    this.confirmDialogOpen.set(false);
    this.confirmCheckbox.set(false);
    // Reset layer-4 confirmation so a subsequent direct call to
    // submitOrder() (e.g. from a future code path) cannot inherit a
    // sticky true value left behind by a previous open.
    this.confirmPaper.set(false);
    this.confirmCooldownMs.set(0);
    this.clearConfirmTick();
  }

  /**
   * Bound to the native dialog's ``cancel`` event (Escape key) and
   * to its ``close`` event so the signal stays synchronized whatever
   * causes the close.
   */
  onDialogClose(): void {
    if (this.confirmDialogOpen()) {
      this.cancelConfirmDialog();
    }
  }

  async confirmAndSubmit(): Promise<void> {
    if (!this.confirmCanPlace()) return;
    this.confirmPaper.set(true);
    await this.submitOrder();
    if (this.lastAck() !== null && this.placeError() === null) {
      this.cancelConfirmDialog();
    }
  }

  private startConfirmTick(): void {
    this.clearConfirmTick();
    this.confirmTickHandle = setInterval(() => {
      const next = this.confirmCooldownMs() - CONFIRM_DIALOG_TICK_MS;
      if (next <= 0) {
        this.confirmCooldownMs.set(0);
        this.clearConfirmTick();
      } else {
        this.confirmCooldownMs.set(next);
      }
    }, CONFIRM_DIALOG_TICK_MS);
  }

  private clearConfirmTick(): void {
    if (this.confirmTickHandle !== null) {
      clearInterval(this.confirmTickHandle);
      this.confirmTickHandle = null;
    }
  }

  async refreshOpenOrders(): Promise<void> {
    if (!this.health.health()?.connected) return;
    this.openOrdersLoading.set(true);
    this.openOrdersError.set(null);
    try {
      const orders = await this.broker.openOrders();
      this.openOrders.set(orders);
    } catch (err) {
      this.openOrdersError.set(err);
    } finally {
      this.openOrdersLoading.set(false);
    }
  }

  async submitOrder(): Promise<void> {
    if (!this.isPaperConnected() || !this.confirmPaper() || this.submitting()) return;

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
      void this.refreshOpenOrders();
    } catch (err) {
      this.placeError.set(err);
    } finally {
      // Always clear layer-4 confirmation, success or failure. The
      // next submit must come back through the dialog so the cooldown
      // and explicit checkbox tick are re-required.
      this.confirmPaper.set(false);
      this.submitting.set(false);
    }
  }

  async cancel(orderId: number): Promise<void> {
    try {
      await this.broker.cancelOrder(orderId);
      void this.refreshOpenOrders();
    } catch (err) {
      this.openOrdersError.set(err);
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

