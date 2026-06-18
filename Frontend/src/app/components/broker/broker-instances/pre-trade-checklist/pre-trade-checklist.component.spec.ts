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

const LABELS: Record<string, string> = {
  desired_state: 'Bot Intent Set',
  broker_connection: 'Broker Connection Live',
};

function render(opts: {
  status: LiveInstanceStatus;
  gateLabels?: Record<string, string>;
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
  fixture.componentRef.setInput('gateLabels', opts.gateLabels ?? LABELS);
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
    const { el, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-close"]')?.click();
    detectChanges();

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).toBeNull();
  });

  it('lists each failing gate in the dialog body', () => {
    const { el, detectChanges } = render({
      status: makeStatus({
        verdict: 'BLOCKED',
        gates: [
          makeGate({ name: 'desired_state', status: 'fail', detail: 'No intent set' }),
          makeGate({ name: 'broker_connection', status: 'fail', detail: 'gateway down' }),
          makeGate({ name: 'orders_cap', status: 'pass' }),
        ],
      }),
    });
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    expect(el.querySelector('[data-testid="pre-trade-ack-desired_state"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="pre-trade-ack-broker_connection"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="pre-trade-ack-orders_cap"]')).toBeNull();
  });

  it('renders the operator-language label for each failing gate (not the raw name)', () => {
    const { el, detectChanges } = render({
      status: makeStatus({
        verdict: 'BLOCKED',
        gates: [
          makeGate({ name: 'desired_state', status: 'fail', detail: 'No intent set' }),
        ],
      }),
    });
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    const text = el.textContent ?? '';
    expect(text).toContain('Bot Intent Set');
    expect(text).not.toContain('desired_state');
  });

  it('disables the ack button and shows "Acknowledged" after clicking it', () => {
    const { el, detectChanges } = render({
      status: makeStatus({
        verdict: 'BLOCKED',
        gates: [makeGate({ name: 'desired_state', status: 'fail' })],
      }),
    });
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-ack-desired_state"]')?.click();
    detectChanges();

    const ackBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="pre-trade-ack-desired_state"]',
    );
    expect(ackBtn?.disabled).toBe(true);
    expect(ackBtn?.textContent?.trim()).toBe('Acknowledged');
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
    const { el, setStatus, detectChanges } = render({ status: failing });

    // Open dialog and acknowledge the failing gate.
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-ack-desired_state"]')?.click();
    detectChanges();
    expect(
      el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-ack-desired_state"]')?.disabled,
    ).toBe(true);

    // Status flips to all-pass — the gate row disappears.
    setStatus(passing);
    detectChanges();
    expect(el.textContent ?? '').toContain('All gates pass');

    // Status flips back to failing — the ack should be dropped.
    setStatus(failing);
    detectChanges();
    const ackBtn = el.querySelector<HTMLButtonElement>(
      '[data-testid="pre-trade-ack-desired_state"]',
    );
    expect(ackBtn?.disabled).toBe(false);
    expect(ackBtn?.textContent?.trim()).toBe('Acknowledge');
  });

  it('closes the dialog when Escape is pressed inside the dialog', () => {
    const { el, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    const dialog = el.querySelector<HTMLElement>('[data-testid="pre-trade-dialog"]');
    dialog?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    detectChanges();

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).toBeNull();
  });

  it('does NOT close when Escape is dispatched outside the dialog', () => {
    const { el, detectChanges } = render({
      status: makeStatus({ verdict: 'DEGRADED' }),
    });
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    detectChanges();

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    detectChanges();

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).not.toBeNull();
  });
});
