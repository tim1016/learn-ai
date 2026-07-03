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
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { brokerSse, type SseStream } from '../../../services/broker-sse';
import type {
  AccountTruthEvidenceGap,
  AccountTruthExecutionRow,
  AccountTruthOrderRow,
  AccountTruthResponse,
  IbkrOrderAck,
  IbkrOrderEvidenceFields,
  IbkrOrderEvent,
  IbkrOrderSpec,
  IbkrOrderWhatIfPreview,
  OrderAction,
  OrderTimeInForce,
  OrderType,
  SecType,
} from '../../../api/broker-models';
import { fmtCurrency, fmtNumber, fmtSignedNumber, fmtTimestampNy } from '../format';

const ORDER_EVENT_BUFFER = 50;
const CONFIRM_DIALOG_COOLDOWN_MS = 3000;
const CONFIRM_DIALOG_TICK_MS = 100;

interface OrderEventLine extends IbkrOrderEvent {
  /** Pre-rendered ET timestamp string for the log row. */
  displayTs: string;
}

interface LedgerSourceNotice {
  key: string;
  headline: string;
  detail: string;
  tone: 'info' | 'warn';
}

interface LedgerOrderRow {
  order: AccountTruthOrderRow;
  executionIds: string[];
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
  imports: [FormsModule, PageHeaderComponent, SectionErrorComponent, PaperOnlyDirective, RouterLink, ReceiptLabelPipe],
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
  readonly whatIfLoading = signal(false);
  readonly whatIfError = signal<unknown>(null);
  readonly whatIfPreview = signal<IbkrOrderWhatIfPreview | null>(null);
  private readonly whatIfPreviewKey = signal<string | null>(null);
  private whatIfRequestId = 0;

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
      this.hasCurrentWhatIfPreview() &&
      this.whatIfError() === null &&
      !this.whatIfLoading() &&
      !this.submitting() &&
      this.isPaperConnected(),
  );

  // Order ledger
  readonly ledgerLoading = signal(false);
  readonly ledgerError = signal<unknown>(null);
  readonly accountTruth = signal<AccountTruthResponse | null>(null);
  readonly retainedCompletedHistory = signal(false);
  readonly ledgerOrders = computed<AccountTruthOrderRow[]>(
    () => this.accountTruth()?.orders ?? [],
  );
  readonly ledgerRows = computed<LedgerOrderRow[]>(() =>
    this.buildLedgerRows(
      this.ledgerOrders(),
      this.accountTruth()?.executions ?? [],
    ),
  );
  readonly hasVisibleCancelActions = computed(() =>
    this.ledgerOrders().some((order) => order.cancel_action.visible),
  );
  readonly ledgerSourceNotices = computed<LedgerSourceNotice[]>(() =>
    this.buildLedgerSourceNotices(),
  );

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

  private ledgerRefreshRunning = false;
  private ledgerRefreshQueued = false;
  private lastLedgerRefreshEventKey: string | null = null;

  constructor() {
    void this.refreshLedger();
    this.openEventStream();

    // Refresh the account-truth ledger when a new order event arrives.
    // The SSE payload itself is not a ledger source; it is only a nudge
    // to re-sweep the broker projection.
    effect(() => {
      const stream = this.eventStream();
      if (stream === null) return;
      const latest = stream.latest();
      if (latest === null) return;
      const key = this.orderEventKey(latest);
      if (key !== this.lastLedgerRefreshEventKey) {
        this.lastLedgerRefreshEventKey = key;
        void this.refreshLedger();
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
    this.clearWhatIfPreview();
    this.confirmDialogOpen.set(true);
    this.startConfirmTick();
    void this.loadWhatIfPreview();
  }

  cancelConfirmDialog(): void {
    this.confirmDialogOpen.set(false);
    this.confirmCheckbox.set(false);
    // Reset layer-4 confirmation so a subsequent direct call to
    // submitOrder() (e.g. from a future code path) cannot inherit a
    // sticky true value left behind by a previous open.
    this.confirmPaper.set(false);
    this.confirmCooldownMs.set(0);
    this.whatIfRequestId += 1;
    this.whatIfLoading.set(false);
    this.clearWhatIfPreview();
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

  async refreshLedger(): Promise<void> {
    if (!this.health.health()?.connected) return;
    if (this.ledgerRefreshRunning) {
      this.ledgerRefreshQueued = true;
      return;
    }

    this.ledgerRefreshRunning = true;
    this.ledgerLoading.set(true);
    try {
      do {
        this.ledgerRefreshQueued = false;
        this.ledgerError.set(null);
        try {
          const truth = await this.broker.accountTruth();
          this.applyAccountTruth(truth);
        } catch (err) {
          this.ledgerError.set(err);
        }
      } while (this.ledgerRefreshQueued);
    } finally {
      this.ledgerRefreshRunning = false;
      this.ledgerLoading.set(false);
    }
  }

  async submitOrder(): Promise<void> {
    if (
      !this.isPaperConnected() ||
      !this.confirmPaper() ||
      this.submitting() ||
      !this.hasCurrentWhatIfPreview()
    ) return;

    this.submitting.set(true);
    this.placeError.set(null);
    const spec = this.buildOrderSpec(true);

    try {
      const ack = await this.broker.placeOrder(spec);
      this.lastAck.set(ack);
      void this.refreshLedger();
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

  async cancel(row: AccountTruthOrderRow): Promise<void> {
    if (!row.cancel_action.enabled) return;
    try {
      await this.broker.cancelOrder(row.order_id);
      void this.refreshLedger();
    } catch (err) {
      this.ledgerError.set(err);
    }
  }

  async loadWhatIfPreview(): Promise<void> {
    if (!this.isPaperConnected()) return;
    const previewKey = this.currentWhatIfKey();
    const requestId = ++this.whatIfRequestId;
    this.whatIfLoading.set(true);
    this.whatIfError.set(null);
    this.whatIfPreview.set(null);
    this.whatIfPreviewKey.set(null);
    try {
      const preview = await this.broker.orderWhatIf(this.buildOrderSpec(false));
      if (requestId === this.whatIfRequestId && previewKey === this.currentWhatIfKey()) {
        this.whatIfPreview.set(preview);
        this.whatIfPreviewKey.set(previewKey);
      }
    } catch (err) {
      if (requestId === this.whatIfRequestId) {
        this.whatIfError.set(err);
        this.whatIfPreviewKey.set(null);
      }
    } finally {
      if (requestId === this.whatIfRequestId) {
        this.whatIfLoading.set(false);
      }
    }
  }

  private buildOrderSpec(confirmPaper: boolean): IbkrOrderSpec {
    return {
      symbol: this.symbol().toUpperCase(),
      sec_type: this.secType(),
      action: this.action(),
      quantity: this.quantity(),
      order_type: this.orderType(),
      limit_price: this.orderType() === 'LMT' ? this.limitPrice() : null,
      time_in_force: this.tif(),
      confirm_paper: confirmPaper,
      client_order_id: confirmPaper ? cryptoUuid() : null,
      multiplier: 100,
      expiry_ms: this.secType() === 'OPT' ? this.expiryMs() : null,
      strike: this.secType() === 'OPT' ? this.strike() : null,
      right: this.secType() === 'OPT' ? this.right() : null,
      manual_order: true,
    };
  }

  trackLedgerRow = (_: number, row: LedgerOrderRow): string => row.order.lifecycle_id;
  trackEvent = (_: number, e: OrderEventLine): string =>
    this.orderEventKey(e);
  trackLedgerNotice = (_: number, notice: LedgerSourceNotice): string => notice.key;

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

  hasIbkrEvidence(value: IbkrOrderEvidenceFields | IbkrOrderEvent): boolean {
    return Boolean(value.ibkr_evidence);
  }

  canCancelLedgerOrder(row: AccountTruthOrderRow): boolean {
    return row.cancel_action.enabled;
  }

  cancelDisabledReason(row: AccountTruthOrderRow): string | null {
    return row.cancel_action.enabled ? null : row.cancel_action.detail;
  }

  cancelButtonLabel(row: AccountTruthOrderRow): string {
    const reason = this.cancelDisabledReason(row);
    if (reason === null) return `${row.cancel_action.label} order ${row.order_id}`;
    return `Cannot cancel order ${row.order_id}: ${reason}`;
  }

  formatQuantity(value: number | null | undefined): string {
    if (value == null) return '—';
    return fmtNumber(value, Number.isInteger(value) ? 0 : 2);
  }

  ibkrEvidenceJson(value: IbkrOrderEvidenceFields | IbkrOrderEvent): string {
    return JSON.stringify(value.ibkr_evidence ?? null, null, 2);
  }

  private clearWhatIfPreview(): void {
    this.whatIfPreview.set(null);
    this.whatIfPreviewKey.set(null);
    this.whatIfError.set(null);
  }

  private hasCurrentWhatIfPreview(): boolean {
    return this.whatIfPreview() !== null && this.whatIfPreviewKey() === this.currentWhatIfKey();
  }

  private currentWhatIfKey(): string {
    const spec = this.buildOrderSpec(false);
    return JSON.stringify({
      symbol: spec.symbol,
      sec_type: spec.sec_type,
      action: spec.action,
      quantity: spec.quantity,
      order_type: spec.order_type,
      limit_price: spec.limit_price,
      time_in_force: spec.time_in_force,
      multiplier: spec.multiplier,
      expiry_ms: spec.expiry_ms,
      strike: spec.strike,
      right: spec.right,
      manual_order: spec.manual_order ?? false,
    });
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

  private applyAccountTruth(truth: AccountTruthResponse): void {
    const previousTruth = this.accountTruth();
    const previousCompleted =
      previousTruth?.account_id === truth.account_id
        ? previousTruth.orders.filter(
            (order) => order.fact_kind === 'completed_order',
          )
        : [];
    const completedOrdersUnavailable = this.hasEvidenceGap(
      truth.evidence_gaps,
      'completed_orders',
    );
    const nextOrders =
      completedOrdersUnavailable && previousCompleted.length > 0
        ? [
            ...truth.orders.filter((order) => order.fact_kind !== 'completed_order'),
            ...previousCompleted,
          ]
        : truth.orders;

    this.retainedCompletedHistory.set(
      completedOrdersUnavailable && previousCompleted.length > 0,
    );
    this.accountTruth.set({ ...truth, orders: nextOrders });
  }

  private buildLedgerSourceNotices(): LedgerSourceNotice[] {
    const notices: LedgerSourceNotice[] = [];
    const status = this.eventStatus();
    if (status === 'error' || status === 'closed') {
      notices.push({
        key: 'event-stream-unavailable',
        headline: 'Live order stream unavailable',
        detail:
          'The order ledger is not using live stream rows. It is rendered from broker account-truth sweeps; live event timing may be stale until the stream reconnects.',
        tone: 'warn',
      });
    }

    const truth = this.accountTruth();
    const completedGap = this.evidenceGap(truth, 'completed_orders');
    if (completedGap) {
      notices.push({
        key: 'completed-orders-unavailable',
        headline: 'Completed-order history unavailable',
        detail: this.retainedCompletedHistory()
          ? `${completedGap.message} Keeping the last successful completed-order rows while current broker facts refresh.`
          : `${completedGap.message} The ledger is limited to the available current-order projection until completed-order history is reachable.`,
        tone: 'warn',
      });
    }

    const openGap = this.evidenceGap(truth, 'open_orders');
    if (openGap) {
      notices.push({
        key: 'open-orders-unavailable',
        headline: 'Open-order sweep unavailable',
        detail: `${openGap.message} Live open orders may be omitted until the broker sweep succeeds.`,
        tone: 'warn',
      });
    }

    return notices;
  }

  private buildLedgerRows(
    orders: AccountTruthOrderRow[],
    executions: AccountTruthExecutionRow[],
  ): LedgerOrderRow[] {
    const executionIdsByKey = new Map<string, Set<string>>();
    for (const execution of executions) {
      if (!execution.exec_id) continue;
      for (const key of this.ledgerMatchKeys(execution)) {
        const ids = executionIdsByKey.get(key) ?? new Set<string>();
        ids.add(execution.exec_id);
        executionIdsByKey.set(key, ids);
      }
    }

    return orders.map((order) => {
      const executionIds = new Set<string>();
      for (const key of this.ledgerMatchKeys(order)) {
        for (const execId of executionIdsByKey.get(key) ?? []) {
          executionIds.add(execId);
        }
      }

      return {
        order,
        executionIds: [...executionIds].sort(),
      };
    });
  }

  private ledgerMatchKeys(
    value: Pick<
      AccountTruthOrderRow | AccountTruthExecutionRow,
      'order_ref' | 'perm_id' | 'order_id'
    >,
  ): string[] {
    const keys: string[] = [];
    if (value.order_ref) keys.push(`ref:${value.order_ref}`);
    if (value.perm_id !== null) keys.push(`perm:${value.perm_id}`);
    if (value.order_id > 0) keys.push(`order:${value.order_id}`);
    return keys;
  }

  private evidenceGap(
    truth: AccountTruthResponse | null,
    source: string,
  ): AccountTruthEvidenceGap | null {
    return truth?.evidence_gaps.find((gap) => gap.source === source) ?? null;
  }

  private hasEvidenceGap(gaps: AccountTruthEvidenceGap[], source: string): boolean {
    return gaps.some((gap) => gap.source === source);
  }

  private orderEventKey(event: Pick<IbkrOrderEvent, 'order_id' | 'ts_ms' | 'event_type'>): string {
    return `${event.order_id}:${event.ts_ms}:${event.event_type}`;
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
