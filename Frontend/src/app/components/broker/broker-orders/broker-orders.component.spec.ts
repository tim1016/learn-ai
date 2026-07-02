import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, expect, it, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { BrokerService } from '../../../services/broker.service';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerOrdersComponent } from './broker-orders.component';
import type {
  AccountTruthOrderRow,
  IbkrOpenOrder,
  IbkrOrderWhatIfPreview,
} from '../../../api/broker-models';

// jsdom lacks EventSource and (across versions) HTMLDialogElement's
// showModal / close. Stub once at module scope so the broker-orders
// constructor — which opens a real SSE stream and the dialog effect
// can both run without exploding.
beforeAll(() => {
  class StubEventSource {
    readyState = 0;
    onopen: ((this: EventSource, ev: Event) => unknown) | null = null;
    onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null = null;
    onerror: ((this: EventSource, ev: Event) => unknown) | null = null;
    addEventListener(): void { /* no-op */ }
    removeEventListener(): void { /* no-op */ }
    dispatchEvent(): boolean { return true; }
    close(): void { /* no-op */ }
  }
  (globalThis as { EventSource?: unknown }).EventSource = StubEventSource;

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
  accountTruth = vi.fn().mockResolvedValue({
    orders: [],
    final_verdict: 'clean',
    final_severity: 'ok',
    status_label: 'Clean',
    status_detail: 'Required live broker evidence is assigned to known ownership.',
  });
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

function setup(openOrders: IbkrOpenOrder[] = []) {
  const broker = new FakeBrokerService();
  broker.openOrders.mockResolvedValue(openOrders);
  broker.accountTruth.mockResolvedValue({
    orders: openOrders.map((order) => ({
      ...order,
      fact_kind: 'open_order',
      lifecycle_id: `perm:${order.perm_id ?? order.order_id}`,
      lifecycle: 'acknowledged',
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
    })),
    final_verdict: 'clean',
    final_severity: 'ok',
    status_label: 'Clean',
    status_detail: 'Required live broker evidence is assigned to known ownership.',
  });
  const health = new FakeBrokerHealthService();
  TestBed.configureTestingModule({
    providers: [
      { provide: BrokerService, useValue: broker },
      { provide: BrokerHealthService, useValue: health },
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

function accountTruthOrder(overrides: Partial<AccountTruthOrderRow> = {}): AccountTruthOrderRow {
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

async function primeWhatIf(component: BrokerOrdersComponent): Promise<void> {
  await component.loadWhatIfPreview();
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
});

describe('BrokerOrdersComponent — broker provenance', () => {
  it('renders the broker order_ref for open orders when the backend provides it', async () => {
    const { fixture, component } = setup([openOrderWithRef]);
    await component.refreshOpenOrders();
    fixture.detectChanges();

    expect((fixture.nativeElement as HTMLElement).textContent).toContain(
      'learn-ai/test-bot/v1:intent-42',
    );
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
  it('explains disabled foreign and terminal ledger cancels', () => {
    const { component } = setup();

    const foreignReason = component.cancelDisabledReason(accountTruthOrder({
      owner: {
        owner_class: 'foreign_or_unclaimed',
        owner_key: 'foreign_or_unclaimed',
        owner_label: 'Foreign or unclaimed',
        evidence_tier: 'foreign_or_unclaimed',
        evidence_label: 'No known ownership evidence',
        owner_binding_state: 'UNKNOWN',
        severity: 'critical',
      },
    }));
    const terminalReason = component.cancelDisabledReason(accountTruthOrder({
      lifecycle: 'filled',
      remaining: 0,
    }));

    expect(foreignReason).toContain('Foreign or unclaimed');
    expect(terminalReason).toContain('terminal');
  });
});
