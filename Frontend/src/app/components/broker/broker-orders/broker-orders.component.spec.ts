import { type ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { describe, expect, it, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { BrokerService } from '../../../services/broker.service';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerOrdersComponent } from './broker-orders.component';
import type {
  AccountTruthExecutionRow,
  AccountTruthOrderCancelAction,
  AccountTruthOrderCancelReasonCode,
  AccountTruthOrderRow,
  AccountTruthPositionRow,
  AccountTruthResponse,
  IbkrOpenOrder,
  IbkrOrderWhatIfPreview,
} from '../../../api/broker-models';

class StubEventSource {
  static instances: StubEventSource[] = [];

  readonly url: string | URL;
  readyState = 0;
  onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null;
  onerror: ((this: EventSource, ev: Event) => unknown) | null = null;
  private readonly listeners = new Map<string, EventListenerOrEventListenerObject[]>();

  constructor(url: string | URL) {
    this.url = url;
    StubEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    this.listeners.set(
      type,
      (this.listeners.get(type) ?? []).filter((item) => item !== listener),
    );
  }

  dispatchEvent(event: Event): boolean {
    for (const listener of this.listeners.get(event.type) ?? []) {
      if (typeof listener === 'function') {
        listener.call(this as unknown as EventSource, event);
      } else {
        listener.handleEvent(event);
      }
    }
    if (event.type === 'error') this.onerror?.call(this as unknown as EventSource, event);
    if (event.type === 'open') this.onopen?.call(this as unknown as EventSource, event);
    return true;
  }

  emit(type: string, data?: string): void {
    this.dispatchEvent(data === undefined ? new Event(type) : new MessageEvent(type, { data }));
  }

  close(): void { /* no-op */ }
}

// jsdom lacks EventSource and (across versions) HTMLDialogElement's
// showModal / close. Stub once at module scope so the broker-orders
// constructor — which opens a real SSE stream and the dialog effect
// can both run without exploding.
beforeAll(() => {
  (globalThis as { EventSource?: unknown }).EventSource =
    StubEventSource as unknown as typeof EventSource;

  if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    };
  }
  if (typeof HTMLDialogElement.prototype.close !== 'function') {
    HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
      this.removeAttribute('open');
      this.dispatchEvent(new Event('close'));
    };
  }
});

beforeEach(() => {
  StubEventSource.instances = [];
});

class FakeBrokerHealthService {
  readonly isPaperConnected = signal(true);
  readonly bannerState = signal<string | null>(null);
  readonly health = signal({
    connected: true,
    is_paper: true,
    account_id: 'DU1234567',
    mode: 'paper' as const,
    host: '127.0.0.1',
    port: 4002,
    client_id: 1,
    server_version: 178,
  });
}

class FakeBrokerService {
  openOrders = vi.fn().mockResolvedValue([]);
  accountTruth = vi.fn().mockResolvedValue(accountTruthResponse());
  orderWhatIf = vi.fn().mockResolvedValue({
    init_margin_change: 10,
    maint_margin_change: 5,
    equity_with_loan_change: -10,
    commission: 1,
    warning_text: null,
  });
  placeOrder = vi.fn();
  cancelOrder = vi.fn().mockResolvedValue(undefined);
  account = vi.fn();
  positions = vi.fn();
}

function whatIfPreview(overrides: Partial<IbkrOrderWhatIfPreview> = {}): IbkrOrderWhatIfPreview {
  return {
    account_id: 'DU1234567',
    is_paper: true,
    symbol: 'SPY',
    action: 'BUY',
    quantity: 1,
    order_type: 'MKT',
    init_margin_change: 10,
    maint_margin_change: 5,
    equity_with_loan_change: -10,
    commission: 1,
    warning_text: null,
    previewed_at_ms: 1,
    ...overrides,
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup(
  openOrders: IbkrOpenOrder[] = [],
  truthResponses?: AccountTruthResponse[],
  queryParams: Record<string, string> = {},
) {
  const broker = new FakeBrokerService();
  broker.openOrders.mockResolvedValue(openOrders);
  const defaultTruth = accountTruthResponse(openOrders.map(openOrderTruthRow));
  broker.accountTruth.mockResolvedValue(defaultTruth);
  for (const response of truthResponses ?? []) {
    broker.accountTruth.mockResolvedValueOnce(response);
  }
  const health = new FakeBrokerHealthService();
  TestBed.configureTestingModule({
    providers: [
      { provide: BrokerService, useValue: broker },
      { provide: BrokerHealthService, useValue: health },
      {
        provide: ActivatedRoute,
        useValue: {
          snapshot: {
            queryParamMap: convertToParamMap(queryParams),
          },
        },
      },
    ],
  });
  const fixture = TestBed.createComponent(BrokerOrdersComponent);
  fixture.detectChanges();
  return { fixture, broker, health, component: fixture.componentInstance };
}

const openOrderWithRef: IbkrOpenOrder = {
  account_id: 'DU1234567',
  order_id: 42,
  client_id: 1,
  con_id: 756733,
  symbol: 'SPY',
  sec_type: 'STK',
  action: 'BUY',
  quantity: 1,
  order_type: 'MKT',
  time_in_force: 'DAY',
  status: 'Submitted',
  cumulative_filled: 0,
  remaining: 1,
  fetched_at_ms: 1,
  order_ref: 'learn-ai/test-bot/v1:intent-42',
};

function enabledCancelAction(): AccountTruthOrderCancelAction {
  return {
    visible: true,
    enabled: true,
    reason_code: null,
    label: 'Cancel',
    detail: 'Sends an IBKR cancel request for this live open order.',
  };
}

function disabledVisibleCancelAction(
  reasonCode: AccountTruthOrderCancelReasonCode,
  detail: string,
): AccountTruthOrderCancelAction {
  return {
    visible: true,
    enabled: false,
    reason_code: reasonCode,
    label: 'Cannot cancel',
    detail,
  };
}

function hiddenNotOpenCancelAction(): AccountTruthOrderCancelAction {
  return {
    visible: false,
    enabled: false,
    reason_code: 'NOT_OPEN_ORDER',
    label: 'Cannot cancel',
    detail: 'Only live open broker orders can be cancelled.',
  };
}

type AccountTruthOrderFixtureOverrides =
  Partial<Omit<AccountTruthOrderRow, 'cancel_action'>> &
  Pick<AccountTruthOrderRow, 'cancel_action'>;

function accountTruthOrder(overrides: AccountTruthOrderFixtureOverrides): AccountTruthOrderRow {
  return {
    ...openOrderWithRef,
    fact_kind: 'open_order',
    lifecycle_id: 'perm:9001',
    lifecycle: 'acknowledged',
    perm_id: 9001,
    limit_price: null,
    avg_fill_price: null,
    owner: {
      owner_class: 'bot',
      owner_key: 'test-bot',
      owner_label: 'Bot test-bot',
      evidence_tier: 'bot_order_ref',
      evidence_label: 'Bot-stamped order ref',
      owner_binding_state: 'ACTIVE',
      severity: 'ok',
    },
    headline: 'Bot test-bot open order',
    detail: 'Ownership is proven by bot-stamped order ref.',
    ...overrides,
  };
}

function accountTruthExecution(
  overrides: Partial<AccountTruthExecutionRow> = {},
): AccountTruthExecutionRow {
  return {
    fact_kind: 'execution',
    account_id: 'DU1234567',
    exec_id: '00025e7a.6685.exec',
    order_id: 42,
    perm_id: 9001,
    client_id: 1,
    con_id: 756733,
    symbol: 'SPY',
    side: 'BUY',
    order_type: 'MKT',
    quantity: 1,
    price: 450,
    fee: null,
    exec_time_ms: 1,
    observed_at_ms: 1,
    order_ref: 'learn-ai/test-bot/v1:intent-42',
    owner: {
      owner_class: 'bot',
      owner_key: 'test-bot',
      owner_label: 'Bot test-bot',
      evidence_tier: 'bot_order_ref',
      evidence_label: 'Bot-stamped order ref',
      owner_binding_state: 'ACTIVE',
      severity: 'ok',
    },
    headline: 'Bot test-bot execution',
    detail: 'Ownership is proven by bot-stamped order ref.',
    uncertainty_codes: [],
    ...overrides,
  };
}

function accountTruthPosition(
  overrides: Partial<AccountTruthPositionRow> = {},
): AccountTruthPositionRow {
  return {
    fact_kind: 'position',
    account_id: 'DU1234567',
    con_id: 756733,
    symbol: 'SPY',
    sec_type: 'STK',
    quantity: 1,
    avg_cost: 450,
    market_value: 450,
    owner: {
      owner_class: 'bot',
      owner_key: 'test-bot',
      owner_label: 'Bot test-bot',
      evidence_tier: 'bot_order_ref',
      evidence_label: 'Bot-stamped order ref',
      owner_binding_state: 'ACTIVE',
      severity: 'ok',
    },
    headline: 'Bot test-bot current position',
    detail: 'Ownership is proven by bot-stamped order ref.',
    fetched_at_ms: 1,
    ...overrides,
  };
}

function openOrderTruthRow(order: IbkrOpenOrder): AccountTruthOrderRow {
  return accountTruthOrder({
    ...order,
    lifecycle_id: `perm:${order.perm_id ?? order.order_id}`,
    cancel_action: enabledCancelAction(),
  });
}

function accountTruthResponse(
  orders: AccountTruthOrderRow[] = [],
  evidenceGaps: AccountTruthResponse['evidence_gaps'] = [],
  executions: AccountTruthExecutionRow[] = [],
  positions: AccountTruthPositionRow[] = [],
): AccountTruthResponse {
  return {
    account_id: 'DU1234567',
    final_verdict: 'clean',
    final_severity: 'ok',
    status_label: 'Clean',
    status_detail: 'Required live broker evidence is assigned to known ownership.',
    generated_at_ms: 1,
    health: {
      connected: true,
      is_paper: true,
      account_id: 'DU1234567',
      mode: 'paper',
      host: '127.0.0.1',
      port: 4002,
      client_id: 1,
      server_version: 178,
      fetched_at_ms: 1,
      connection_state: 'connected',
      last_transition_ms: 1,
    },
    account: null,
    known_bot_namespaces: [],
    manual_namespaces_observed: [],
    invariants: [],
    blockers: [],
    operator_blockers: [],
    caveats: [],
    owner_summaries: [],
    symbol_exposures: [],
    orders,
    executions,
    positions,
    evidence_gaps: evidenceGaps,
    source_freshness: [],
  };
}

function orderLedgerElement(fixture: ComponentFixture<BrokerOrdersComponent>): HTMLElement {
  return (fixture.nativeElement as HTMLElement).querySelector('.orders-card') as HTMLElement;
}

function orderLedgerText(fixture: ComponentFixture<BrokerOrdersComponent>): string {
  return orderLedgerElement(fixture).textContent ?? '';
}

async function primeWhatIf(component: BrokerOrdersComponent): Promise<void> {
  await component.loadWhatIfPreview();
}

async function flushAsyncWork(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('BrokerOrdersComponent — confirm dialog accessibility', () => {
  let showModal: ReturnType<typeof vi.spyOn>;
  let close: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // jsdom's HTMLDialogElement support varies across versions — spy
    // on showModal and close so the test pins behavior without
    // depending on jsdom's native focus implementation.
    showModal = vi
      .spyOn(HTMLDialogElement.prototype, 'showModal')
      .mockImplementation(function (this: HTMLDialogElement) {
        this.setAttribute('open', '');
      });
    close = vi
      .spyOn(HTMLDialogElement.prototype, 'close')
      .mockImplementation(function (this: HTMLDialogElement) {
        this.removeAttribute('open');
        this.dispatchEvent(new Event('close'));
      });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('calls showModal() on the native dialog when openConfirmDialog runs', () => {
    const { fixture, component } = setup();
    component.openConfirmDialog();
    fixture.detectChanges();
    expect(showModal).toHaveBeenCalledTimes(1);
  });

  it('calls close() on the native dialog when cancelConfirmDialog runs', () => {
    const { fixture, component } = setup();
    component.openConfirmDialog();
    fixture.detectChanges();
    component.cancelConfirmDialog();
    fixture.detectChanges();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('resets confirmPaper when the dialog is cancelled', () => {
    const { fixture, component } = setup();
    component.openConfirmDialog();
    fixture.detectChanges();
    component.confirmPaper.set(true);
    component.cancelConfirmDialog();
    expect(component.confirmPaper()).toBe(false);
  });

  it('resets dialog state when the native close event fires (Escape key path)', () => {
    const { fixture, component } = setup();
    component.openConfirmDialog();
    fixture.detectChanges();
    expect(component.confirmDialogOpen()).toBe(true);

    const dialog = (fixture.nativeElement as HTMLElement).querySelector(
      'dialog',
    ) as HTMLDialogElement;
    dialog.dispatchEvent(new Event('cancel'));
    expect(component.confirmDialogOpen()).toBe(false);
    expect(component.confirmPaper()).toBe(false);
  });

  it('clears placeError when the dialog is reopened', () => {
    const { fixture, component } = setup();
    component['placeError'].set(new Error('previous fail'));
    component.openConfirmDialog();
    fixture.detectChanges();
    expect(component['placeError']()).toBeNull();
  });

  it('explains why the final place action is disabled', async () => {
    const { fixture, component } = setup();
    component.openConfirmDialog();
    await flushAsyncWork();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Tick the paper-order confirmation checkbox.');

    component.confirmCheckbox.set(true);
    fixture.detectChanges();

    const checkedText = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(checkedText).toContain('Safety pause:');
    component.cancelConfirmDialog();
  });
});

describe('BrokerOrdersComponent — broker provenance', () => {
  it('renders app order refs and broker execution IDs without promoting zero API order IDs', async () => {
    const order = accountTruthOrder({
      fact_kind: 'completed_order',
      lifecycle_id: 'perm:9001',
      order_id: 0,
      perm_id: 9001,
      status: 'Filled',
      quantity: 1,
      cumulative_filled: 1,
      remaining: 0,
      cancel_action: hiddenNotOpenCancelAction(),
    });
    const execution = accountTruthExecution({
      order_id: 0,
      perm_id: 9001,
    });
    const { fixture } = setup([], [
      accountTruthResponse([order], [], [execution]),
    ]);
    await flushAsyncWork();
    fixture.detectChanges();

    const headers = Array.from(
      orderLedgerElement(fixture).querySelectorAll('th'),
      (header) => header.textContent?.trim() ?? '',
    );
    expect(headers).toContain('Order ref');
    expect(headers).toContain('Broker order');
    expect(headers).toContain('Broker exec ID');
    expect(headers).not.toContain('ID');
    expect(headers).not.toContain('Owner');
    expect(headers).not.toContain('Qty');
    expect(headers).not.toContain('Status');

    const ledgerText = orderLedgerText(fixture);
    expect(ledgerText).toContain('learn-ai/test-bot/v1:intent-42');
    expect(ledgerText).toContain('Perm 9001');
    expect(ledgerText).toContain('00025e7a.6685.exec');
    expect(ledgerText).toContain('Broker direct');
    expect(ledgerText).toContain('Stamped and echoed');
    expect(ledgerText).toContain('1/1');
    expect(ledgerText).not.toContain('Completed Order');
    expect(ledgerText).not.toContain('Filled');
    expect(ledgerText).not.toContain('IBKR');

    const cellTexts = Array.from(
      orderLedgerElement(fixture).querySelectorAll('tbody td'),
      (cell) => cell.textContent?.trim() ?? '',
    );
    expect(cellTexts).not.toContain('0');
    expect(
      orderLedgerElement(fixture).querySelectorAll('.source-legend .source-pill'),
    ).toHaveLength(2);
    expect(orderLedgerElement(fixture).querySelector('tbody .source-pill')).toBeNull();
    expect(orderLedgerElement(fixture).querySelector('details.ibkr-evidence')).toBeNull();
    expect(orderLedgerElement(fixture).querySelector('button[aria-label^="Cancel"]')).toBeNull();
  });

  it('uses account truth as the ledger source without the unused open-orders call', async () => {
    const { broker, component } = setup([openOrderWithRef]);

    await flushAsyncWork();
    await component.refreshLedger();

    expect(broker.accountTruth).toHaveBeenCalled();
    expect(broker.openOrders).not.toHaveBeenCalled();
  });

  it('keeps completed history stable when the completed-order sweep degrades', async () => {
    const openOrder = accountTruthOrder({
      fact_kind: 'open_order',
      lifecycle_id: 'perm:9001',
      order_id: 42,
      order_ref: 'learn-ai/test-bot/v1:open-42',
      perm_id: 9001,
      status: 'Submitted',
      remaining: 1,
      cancel_action: enabledCancelAction(),
    });
    const completedOrder = accountTruthOrder({
      fact_kind: 'completed_order',
      lifecycle_id: 'perm:9002',
      order_id: 43,
      order_ref: 'learn-ai/test-bot/v1:completed-43',
      perm_id: 9002,
      status: 'Filled',
      quantity: 1,
      cumulative_filled: 1,
      remaining: 0,
      cancel_action: hiddenNotOpenCancelAction(),
    });
    const { fixture, component } = setup([], [
      accountTruthResponse([openOrder, completedOrder]),
      accountTruthResponse([openOrder], [
        {
          source: 'completed_orders',
          severity: 'warning',
          message: 'IBKR completed-order sweep unavailable.',
        },
      ]),
    ]);

    await flushAsyncWork();
    await component.refreshLedger();
    fixture.detectChanges();

    const text = orderLedgerText(fixture);
    expect(text).toContain('learn-ai/test-bot/v1:open-42');
    expect(text).toContain('learn-ai/test-bot/v1:completed-43');
    expect(text).toContain('Perm 9002');
    expect(text).toContain('Completed-order history unavailable');
    expect(text).toContain('Keeping the last successful completed-order rows');
  });

  it('explains that the ledger falls back to sweeps when the stream is unavailable', async () => {
    const { fixture, component } = setup([openOrderWithRef]);

    await flushAsyncWork();
    await component.refreshLedger();
    StubEventSource.instances[0]?.emit('error');
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Live order stream unavailable');
    expect(text).toContain('not using live stream rows');
    expect(text).toContain('learn-ai/test-bot/v1:intent-42');
  });

  it('shows COI guidance and pre-fills the flatten cure for one stock position', async () => {
    const { fixture, broker, component } = setup([], [
      accountTruthResponse([], [], [], [accountTruthPosition()]),
    ]);

    await flushAsyncWork();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('COI guidance');
    expect(text).toContain('Current open inventory: SPY +1');
    expect(text).toContain('Cure: prefill SELL 1 SPY MKT DAY');

    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((candidate) => candidate.textContent?.includes('Prefill flatten order'));
    expect(button).toBeTruthy();
    component.prefillOpenExposureCure();
    fixture.detectChanges();

    expect(component.action()).toBe('SELL');
    expect(component.quantity()).toBe(1);
    expect(
      ((fixture.nativeElement as HTMLElement).querySelector('[name="symbol"]') as HTMLInputElement)
        .value,
    ).toBe('SPY');
    expect(broker.placeOrder).not.toHaveBeenCalled();
  });

  it('auto-prefills the flatten cure from the legacy deep link', async () => {
    const { fixture, broker, component } = setup(
      [],
      [accountTruthResponse([], [], [], [accountTruthPosition()])],
      { flatten: 'open-exposure' },
    );

    await flushAsyncWork();
    fixture.detectChanges();

    expect(component.action()).toBe('SELL');
    expect(component.quantity()).toBe(1);
    expect(
      ((fixture.nativeElement as HTMLElement).querySelector('[name="symbol"]') as HTMLInputElement)
        .value,
    ).toBe('SPY');
    expect(broker.placeOrder).not.toHaveBeenCalled();
  });
});

describe('BrokerOrdersComponent — confirmPaper failure-reset', () => {
  it('clears confirmPaper after a failed place even though placeError is set', async () => {
    const { component, broker } = setup();
    broker.placeOrder.mockRejectedValueOnce(new Error('rejected'));
    await primeWhatIf(component);
    component.confirmPaper.set(true);
    await component.submitOrder();
    expect(component.confirmPaper()).toBe(false);
    expect(component['placeError']()).toBeInstanceOf(Error);
  });

  it('clears confirmPaper after a successful place', async () => {
    const { component, broker } = setup();
    broker.placeOrder.mockResolvedValueOnce({
      order_id: 1,
      status: 'submitted',
      placed_at_ms: Date.now(),
    });
    await primeWhatIf(component);
    component.confirmPaper.set(true);
    await component.submitOrder();
    expect(component.confirmPaper()).toBe(false);
  });

  it('renders the place response as non-terminal acknowledgement copy', async () => {
    const { fixture, component, broker } = setup();
    broker.placeOrder.mockResolvedValueOnce({
      order_id: 1,
      status: 'Submitted',
      placed_at_ms: 1_780_000_000_000,
      order_ref: 'manual/operator/v1:intent-1',
    });
    await primeWhatIf(component);
    component.confirmPaper.set(true);

    await component.submitOrder();
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Order request acknowledged by IBKR API.');
    expect(text).toContain('Verify the terminal state in the order ledger or live order events.');
    expect(text).not.toContain('Placed order');
  });

  it('marks form submits as manual orders so the server mints order_ref', async () => {
    const { component, broker } = setup();
    broker.placeOrder.mockResolvedValueOnce({
      order_id: 1,
      status: 'Submitted',
      placed_at_ms: Date.now(),
      order_ref: 'manual/operator/v1:intent-1',
    });
    await primeWhatIf(component);
    component.confirmPaper.set(true);

    await component.submitOrder();

    const spec = broker.placeOrder.mock.calls[0][0];
    expect(spec.manual_order).toBe(true);
    expect(spec.order_ref).toBeUndefined();
  });

  it('refuses to submit when confirmPaper is false', async () => {
    const { component, broker } = setup();
    component.confirmPaper.set(false);
    await component.submitOrder();
    expect(broker.placeOrder).not.toHaveBeenCalled();
  });
});

describe('BrokerOrdersComponent — what-if preview gate', () => {
  it('ignores stale what-if responses', async () => {
    const { component, broker } = setup();
    const first = deferred<IbkrOrderWhatIfPreview>();
    const second = deferred<IbkrOrderWhatIfPreview>();
    broker.orderWhatIf
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const firstLoad = component.loadWhatIfPreview();
    component.quantity.set(2);
    const secondLoad = component.loadWhatIfPreview();

    second.resolve(whatIfPreview({ commission: 2, quantity: 2 }));
    await secondLoad;
    first.resolve(whatIfPreview({ commission: 1, quantity: 1 }));
    await firstLoad;

    expect(component.whatIfPreview()?.commission).toBe(2);
  });

  it('disables confirmation when the form changes after preview', async () => {
    const { component } = setup();
    await primeWhatIf(component);

    component.confirmDialogOpen.set(true);
    component.confirmCheckbox.set(true);
    component.confirmCooldownMs.set(0);
    expect(component.confirmCanPlace()).toBe(true);

    component.quantity.set(2);
    expect(component.confirmCanPlace()).toBe(false);
  });
});

describe('BrokerOrdersComponent — cancel reasons', () => {
  it('uses backend-authored disabled cancel detail', () => {
    const { component } = setup();

    const foreignReason = component.cancelDisabledReason(accountTruthOrder({
      cancel_action: disabledVisibleCancelAction(
        'FOREIGN_OR_UNCLAIMED',
        'Backend says this order needs adoption first.',
      ),
    }));
    const terminalReason = component.cancelDisabledReason(accountTruthOrder({
      cancel_action: disabledVisibleCancelAction(
        'ORDER_TERMINAL',
        'Backend says this order is terminal.',
      ),
    }));

    expect(foreignReason).toBe('Backend says this order needs adoption first.');
    expect(terminalReason).toBe('Backend says this order is terminal.');
  });

  it('does not cancel when the backend action is disabled', async () => {
    const { component, broker } = setup();

    await component.cancel(accountTruthOrder({
      cancel_action: disabledVisibleCancelAction(
        'BROKER_NOT_PAPER_CONNECTED',
        'Backend says paper broker is unavailable.',
      ),
    }));

    expect(broker.cancelOrder).not.toHaveBeenCalled();
  });
});
