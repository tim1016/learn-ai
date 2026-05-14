import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { describe, expect, it, vi, beforeAll, beforeEach, afterEach } from 'vitest';
import { BrokerService } from '../../../services/broker.service';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerOrdersComponent } from './broker-orders.component';

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
  placeOrder = vi.fn();
  cancelOrder = vi.fn().mockResolvedValue(undefined);
  account = vi.fn();
  positions = vi.fn();
}

function setup() {
  const broker = new FakeBrokerService();
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

describe('BrokerOrdersComponent — confirmPaper failure-reset', () => {
  it('clears confirmPaper after a failed place even though placeError is set', async () => {
    const { component, broker } = setup();
    broker.placeOrder.mockRejectedValueOnce(new Error('rejected'));
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
    component.confirmPaper.set(true);
    await component.submitOrder();
    expect(component.confirmPaper()).toBe(false);
  });

  it('refuses to submit when confirmPaper is false', async () => {
    const { component, broker } = setup();
    component.confirmPaper.set(false);
    await component.submitOrder();
    expect(broker.placeOrder).not.toHaveBeenCalled();
  });
});
