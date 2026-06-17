import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReadinessGate, ReadinessVector } from '../../../../api/live-instances.types';
import { ReadinessCardComponent } from './readiness-card.component';

const LABELS: Record<string, string> = {
  desired_state: 'Bot Intent Set',
  poison_sentinel: 'No Emergency Stop',
  orders_cap: 'Daily Trade Limit Available',
  broker_connection: 'Broker Connection Live',
};

function makeGate(overrides: Partial<ReadinessGate> = {}): ReadinessGate {
  return {
    name: 'desired_state',
    status: 'pass',
    severity: 'hard',
    detail: '',
    ...overrides,
  };
}

function makeReadiness(
  verdict: ReadinessVector['verdict'],
  gates: ReadinessGate[],
): ReadinessVector {
  return {
    kind: 'live_readiness',
    as_of_ms: 0,
    source: 'engine',
    verdict,
    summary: '',
    gates,
  };
}

function render(opts: {
  readiness: ReadinessVector | null;
  gateLabels?: Record<string, string>;
}): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ReadinessCardComponent);
  fixture.componentRef.setInput('readiness', opts.readiness);
  fixture.componentRef.setInput('gateLabels', opts.gateLabels ?? LABELS);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('ReadinessCardComponent', () => {
  it('renders the calm "READY · N checks pass" strip when verdict is READY (User Story #14)', () => {
    const el = render({
      readiness: makeReadiness('READY', [
        makeGate({ name: 'desired_state' }),
        makeGate({ name: 'poison_sentinel' }),
      ]),
    });

    const strip = el.querySelector<HTMLElement>('[data-testid="readiness-ready-strip"]');
    expect(strip).not.toBeNull();
    expect(strip?.textContent ?? '').toContain('READY');
    expect(strip?.textContent ?? '').toContain('2 / 2 checks pass');
    // The strip is *not* the full card.
    expect(el.querySelector('[data-testid="readiness-card"]')).toBeNull();
  });

  it('renders the full card when verdict is BLOCKED', () => {
    const el = render({
      readiness: makeReadiness('BLOCKED', [
        makeGate({ name: 'desired_state', status: 'fail', detail: 'No intent set' }),
        makeGate({ name: 'poison_sentinel' }),
      ]),
    });

    const card = el.querySelector<HTMLElement>('[data-testid="readiness-card"]');
    expect(card).not.toBeNull();
    expect(card?.classList.contains('bad')).toBe(true);
    expect(
      el.querySelector('[data-testid="readiness-verdict-chip"]')?.textContent?.trim(),
    ).toBe('BLOCKED');
  });

  it('shows the proportional X / N count (User Story #57)', () => {
    const el = render({
      readiness: makeReadiness('DEGRADED', [
        makeGate({ name: 'desired_state', status: 'pass' }),
        makeGate({ name: 'poison_sentinel', status: 'pass' }),
        makeGate({ name: 'orders_cap', status: 'fail', severity: 'soft', detail: '3 of 3 used' }),
        makeGate({ name: 'broker_connection', status: 'fail', severity: 'hard', detail: 'gateway down' }),
      ]),
    });

    expect(
      el.querySelector('[data-testid="readiness-proportion"]')?.textContent?.trim(),
    ).toBe('2 / 4 checks pass');
  });

  it('lists failing gates with operator-language labels and severity badges', () => {
    const el = render({
      readiness: makeReadiness('BLOCKED', [
        makeGate({
          name: 'broker_connection',
          status: 'fail',
          severity: 'hard',
          detail: 'IBKR gateway not reachable',
        }),
        makeGate({
          name: 'orders_cap',
          status: 'fail',
          severity: 'soft',
          detail: '3 of 3 daily orders used',
        }),
      ]),
    });

    const text = el.textContent ?? '';
    expect(text).toContain('Broker Connection Live');
    expect(text).toContain('Blocking');
    expect(text).toContain('Daily Trade Limit Available');
    expect(text).toContain('Advisory');
    expect(text).toContain('IBKR gateway not reachable');
    expect(text).toContain('3 of 3 daily orders used');
  });

  it('falls back to the raw gate name when the label map is missing an entry', () => {
    const el = render({
      readiness: makeReadiness('BLOCKED', [
        makeGate({ name: 'unknown_gate', status: 'fail', detail: 'something' }),
      ]),
      gateLabels: {},
    });

    expect(el.textContent ?? '').toContain('unknown_gate');
  });

  it('renders the NO_READINESS warn state when readiness is null', () => {
    const el = render({ readiness: null });

    const chip = el.querySelector<HTMLElement>(
      '[data-testid="readiness-verdict-chip"]',
    );
    expect(chip?.textContent?.trim()).toBe('NO READINESS');
    expect(el.textContent ?? '').toContain('engine has not emitted a readiness vector');
  });

  it('renders the warn-tone card when verdict is DEGRADED', () => {
    const el = render({
      readiness: makeReadiness('DEGRADED', [
        makeGate({ name: 'desired_state', status: 'pass' }),
        makeGate({
          name: 'orders_cap',
          status: 'fail',
          severity: 'soft',
          detail: '3 of 3 used',
        }),
      ]),
    });

    const card = el.querySelector<HTMLElement>('[data-testid="readiness-card"]');
    expect(card?.classList.contains('warn')).toBe(true);
  });
});
