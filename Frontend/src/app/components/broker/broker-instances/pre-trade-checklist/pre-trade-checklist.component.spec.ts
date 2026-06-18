import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  LiveInstanceStatus,
  ReadinessGate,
  ReadinessVerdict,
} from '../../../../api/live-instances.types';

import { PreTradeChecklistComponent } from './pre-trade-checklist.component';

function makeGate(overrides: Partial<ReadinessGate> = {}): ReadinessGate {
  return {
    name: 'desired_state',
    status: 'fail',
    severity: 'hard',
    detail: 'No intent set',
    ...overrides,
  };
}

function makeStatus(opts: {
  verdict?: ReadinessVerdict;
  gates?: ReadinessGate[];
}): LiveInstanceStatus {
  return {
    readiness: opts.verdict
      ? {
          kind: 'live_readiness',
          as_of_ms: 0,
          source: 'engine',
          verdict: opts.verdict,
          summary: '',
          gates: opts.gates ?? [],
        }
      : null,
  } as unknown as LiveInstanceStatus;
}

function render(opts: {
  status: LiveInstanceStatus;
}): {
  el: HTMLElement;
  component: PreTradeChecklistComponent;
  setStatus: (s: LiveInstanceStatus) => void;
  detectChanges: () => void;
} {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(PreTradeChecklistComponent);
  fixture.componentRef.setInput('status', opts.status);
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
    setStatus: (s) => fixture.componentRef.setInput('status', s),
    detectChanges: () => fixture.detectChanges(),
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('PreTradeChecklistComponent', () => {
  it('hides the FAB when fleet state is STEADY (readiness READY)', () => {
    const { el } = render({ status: makeStatus({ verdict: 'READY' }) });

    expect(el.querySelector('[data-testid="pre-trade-fab"]')).toBeNull();
  });

  it('shows the FAB when fleet state is CONFIGURE (readiness DEGRADED)', () => {
    const { el } = render({ status: makeStatus({ verdict: 'DEGRADED' }) });

    expect(el.querySelector('[data-testid="pre-trade-fab"]')).not.toBeNull();
  });

  it('shows the FAB when fleet state is BLOCKED', () => {
    const { el } = render({ status: makeStatus({ verdict: 'BLOCKED' }) });

    expect(el.querySelector('[data-testid="pre-trade-fab"]')).not.toBeNull();
  });

  it('opens the dialog when the FAB is clicked', () => {
    const { el, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).toBeNull();
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).not.toBeNull();
  });

  it('closes the dialog when the close button is clicked', () => {
    const { el, component, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });
    component.toggleOpen();
    detectChanges();

    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-close"]')?.click();

    expect(component.open()).toBe(false);
  });

  it('lists each failing gate in the dialog body', () => {
    const { el, component, detectChanges } = render({
      status: makeStatus({
        verdict: 'BLOCKED',
        gates: [
          makeGate({ name: 'desired_state', status: 'fail', detail: 'No intent set' }),
          makeGate({ name: 'broker_connection', status: 'fail', detail: 'gateway down' }),
          makeGate({ name: 'orders_cap', status: 'pass' }),
        ],
      }),
    });
    component.toggleOpen();
    detectChanges();

    const text = el.textContent ?? '';
    expect(text).toContain('desired_state');
    expect(text).toContain('broker_connection');
    expect(text).not.toContain('orders_cap');
  });

  it('marks a gate acknowledged when its Acknowledge button is clicked', () => {
    const { el, component, detectChanges } = render({
      status: makeStatus({
        verdict: 'BLOCKED',
        gates: [makeGate({ name: 'desired_state', status: 'fail' })],
      }),
    });
    component.toggleOpen();
    detectChanges();

    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-ack-desired_state"]')?.click();

    expect(component.acknowledged().has('desired_state')).toBe(true);
  });

  it('drops an ack when the gate stops failing and reappears failing later', () => {
    const failing = makeStatus({
      verdict: 'BLOCKED',
      gates: [makeGate({ name: 'desired_state', status: 'fail' })],
    });
    const passing = makeStatus({
      verdict: 'READY',
      gates: [makeGate({ name: 'desired_state', status: 'pass' })],
    });
    const { component, setStatus, detectChanges } = render({ status: failing });

    component.acknowledge('desired_state');
    expect(component.acknowledged().has('desired_state')).toBe(true);

    setStatus(passing);
    detectChanges();
    expect(component.acknowledged().has('desired_state')).toBe(false);

    setStatus(failing);
    detectChanges();
    expect(component.acknowledged().has('desired_state')).toBe(false);
  });

  it('closes the dialog when Escape is pressed inside the dialog', () => {
    const { el, component, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });
    component.toggleOpen();
    detectChanges();

    const dialog = el.querySelector<HTMLElement>('[data-testid="pre-trade-dialog"]');
    dialog?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

    expect(component.open()).toBe(false);
  });

  it('does NOT close when Escape is dispatched outside the dialog', () => {
    const { component, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });
    component.toggleOpen();
    detectChanges();

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));

    expect(component.open()).toBe(true);
  });
});
