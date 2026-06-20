import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { FailingGateRow } from '../failing-gates';
import { ChecklistDialogComponent } from './checklist-dialog.component';

function render(opts: {
  open: boolean;
  failingGates?: FailingGateRow[];
  acknowledged?: ReadonlySet<string>;
}) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ChecklistDialogComponent);
  fixture.componentRef.setInput('open', opts.open);
  fixture.componentRef.setInput('failingGates', opts.failingGates ?? []);
  fixture.componentRef.setInput('acknowledged', opts.acknowledged ?? new Set<string>());
  let acks = 0;
  let lastAck: string | null = null;
  let closes = 0;
  fixture.componentInstance.acknowledge.subscribe((k) => {
    acks += 1;
    lastAck = k;
  });
  fixture.componentInstance.closeRequested.subscribe(() => (closes += 1));
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    get acks() {
      return acks;
    },
    get lastAck() {
      return lastAck;
    },
    get closes() {
      return closes;
    },
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('ChecklistDialogComponent', () => {
  it('renders nothing when open is false', () => {
    const h = render({ open: false });
    expect(h.el.querySelector('[data-testid="pre-trade-dialog"]')).toBeNull();
  });

  it('renders the dialog with aria-labelledby pointing at the heading id', () => {
    const h = render({ open: true });
    const dialog = h.el.querySelector('[data-testid="pre-trade-dialog"]');
    expect(dialog?.getAttribute('aria-labelledby')).toBe('pre-trade-dialog-heading');
    expect(h.el.querySelector('#pre-trade-dialog-heading')).not.toBeNull();
  });

  it('emits closeRequested on close-button click', () => {
    const h = render({ open: true });
    h.el.querySelector<HTMLButtonElement>('[data-testid="pre-trade-close"]')?.click();
    expect(h.closes).toBe(1);
  });

  it('emits acknowledge with the gate key when the Ack button is clicked', () => {
    const gates: FailingGateRow[] = [
      { key: 'broker_connection', label: 'Broker Connection', detail: 'down', severity: 'hard' },
    ];
    const h = render({ open: true, failingGates: gates });
    h.el
      .querySelector<HTMLButtonElement>('[data-testid="pre-trade-ack-broker_connection"]')
      ?.click();
    expect(h.acks).toBe(1);
    expect(h.lastAck).toBe('broker_connection');
  });

  it('shows the empty-state message when no gates are failing', () => {
    const h = render({ open: true, failingGates: [] });
    expect(h.el.textContent ?? '').toContain('All gates pass');
  });

  it('disables the Ack button when the gate is already acknowledged', () => {
    const gates: FailingGateRow[] = [
      { key: 'orders_cap', label: 'Orders Cap', detail: '5/5', severity: 'hard' },
    ];
    const h = render({
      open: true,
      failingGates: gates,
      acknowledged: new Set(['orders_cap']),
    });
    const btn = h.el.querySelector<HTMLButtonElement>(
      '[data-testid="pre-trade-ack-orders_cap"]',
    );
    expect(btn?.disabled).toBe(true);
    expect(btn?.textContent?.trim()).toBe('Acknowledged');
  });
});
