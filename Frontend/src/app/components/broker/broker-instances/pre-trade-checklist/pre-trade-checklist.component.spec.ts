import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReadinessGate } from '../../../../api/live-instances.types';

import { PreTradeChecklistComponent } from './pre-trade-checklist.component';
import type { FleetState } from '../sticky-control-bar/fleet-state';

function makeGate(overrides: Partial<ReadinessGate> = {}): ReadinessGate {
  return {
    name: 'desired_state',
    status: 'fail',
    severity: 'hard',
    detail: 'No intent set',
    ...overrides,
  };
}

function render(opts: {
  fleetState?: FleetState;
  gates?: ReadinessGate[];
}): { el: HTMLElement; component: PreTradeChecklistComponent } {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(PreTradeChecklistComponent);
  fixture.componentRef.setInput('fleetState', opts.fleetState ?? 'CONFIGURE');
  fixture.componentRef.setInput('gates', opts.gates ?? []);
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('PreTradeChecklistComponent', () => {
  it('hides the FAB when fleet state is STEADY', () => {
    const { el } = render({ fleetState: 'STEADY' });

    expect(el.querySelector('[data-testid="pre-trade-fab"]')).toBeNull();
  });

  it('shows the FAB when fleet state is CONFIGURE', () => {
    const { el } = render({ fleetState: 'CONFIGURE' });

    expect(el.querySelector('[data-testid="pre-trade-fab"]')).not.toBeNull();
  });

  it('shows the FAB when fleet state is BLOCKED', () => {
    const { el } = render({ fleetState: 'BLOCKED' });

    expect(el.querySelector('[data-testid="pre-trade-fab"]')).not.toBeNull();
  });

  it('opens the dialog when the FAB is clicked', () => {
    const { el } = render({ fleetState: 'CONFIGURE' });

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).toBeNull();
    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-fab"]')?.click();
    TestBed.tick();

    expect(el.querySelector('[data-testid="pre-trade-dialog"]')).not.toBeNull();
  });

  it('closes the dialog when the close button is clicked', () => {
    const { el, component } = render({ fleetState: 'CONFIGURE' });
    component.toggleOpen();
    TestBed.tick();

    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-close"]')?.click();

    expect(component.open()).toBe(false);
  });

  it('lists each failing gate in the dialog body', () => {
    const { el, component } = render({
      fleetState: 'BLOCKED',
      gates: [
        makeGate({ name: 'desired_state', status: 'fail', detail: 'No intent set' }),
        makeGate({ name: 'broker_connection', status: 'fail', detail: 'gateway down' }),
        makeGate({ name: 'orders_cap', status: 'pass' }),
      ],
    });
    component.toggleOpen();
    TestBed.tick();

    const text = el.textContent ?? '';
    expect(text).toContain('desired_state');
    expect(text).toContain('broker_connection');
    expect(text).not.toContain('orders_cap');
  });

  it('marks a gate acknowledged when its Acknowledge button is clicked', () => {
    const { el, component } = render({
      fleetState: 'BLOCKED',
      gates: [makeGate({ name: 'desired_state', status: 'fail' })],
    });
    component.toggleOpen();
    TestBed.tick();

    el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-ack-desired_state"]')?.click();

    expect(component.acknowledged().has('desired_state')).toBe(true);
  });

  it('closes the dialog when Escape is pressed', () => {
    const { component } = render({ fleetState: 'CONFIGURE' });
    component.toggleOpen();
    expect(component.open()).toBe(true);

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));

    expect(component.open()).toBe(false);
  });
});
