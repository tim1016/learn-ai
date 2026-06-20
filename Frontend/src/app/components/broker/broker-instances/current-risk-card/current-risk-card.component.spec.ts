import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  InstanceBrokerView,
  InstanceSizing,
  OperatorSurfaceCurrentRisk,
  ReadinessVector,
} from '../../../../api/live-instances.types';
import { CurrentRiskCardComponent } from './current-risk-card.component';

const DEFAULT_RISK: OperatorSurfaceCurrentRisk = {
  posture: 'FLAT',
  pending_order_count: 0,
  verdict: 'READY',
  unrealized_pnl: null,
};

function makeBroker(
  overrides: Partial<InstanceBrokerView> = {},
): InstanceBrokerView {
  return {
    bot_order_namespace: 'spy_15m_breakout',
    owned_positions: {},
    pending_order_count: 0,
    ...overrides,
  };
}

function makeReadiness(
  gates: ReadinessVector['gates'] = [],
): ReadinessVector {
  return {
    kind: 'live_readiness',
    as_of_ms: 0,
    source: 'engine',
    verdict: 'READY',
    summary: '',
    gates,
  };
}

function makeSizing(overrides: Partial<InstanceSizing> = {}): InstanceSizing {
  return {
    policy: null,
    preset: 'safe_canary',
    governed_by: 'live_config',
    sizing_provenance: 'reference_native',
    per_trade_audit: [],
    ...overrides,
  };
}

function render(opts: {
  broker: InstanceBrokerView | null;
  readiness: ReadinessVector | null;
  sizing: InstanceSizing | null;
  currentRisk?: OperatorSurfaceCurrentRisk;
}): HTMLElement {
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(CurrentRiskCardComponent);
  fixture.componentRef.setInput('broker', opts.broker);
  fixture.componentRef.setInput('readiness', opts.readiness);
  fixture.componentRef.setInput('sizing', opts.sizing);
  fixture.componentRef.setInput('currentRisk', opts.currentRisk ?? DEFAULT_RISK);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('CurrentRiskCardComponent', () => {
  it('explicitly renders "Flat · 0 positions" when nothing is held (User Story #17)', () => {
    const el = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const chip = el.querySelector<HTMLElement>('[data-testid="posture-chip"]');
    expect(chip?.textContent?.trim()).toBe('Flat · 0 positions');
    expect(chip?.classList.contains('flat')).toBe(true);
    expect(el.querySelector('[data-testid="positions-empty"]')).not.toBeNull();
  });

  it('renders long posture and the position pill in the long tone', () => {
    const el = render({
      broker: makeBroker({ owned_positions: { SPY: 100 } }),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const chip = el.querySelector<HTMLElement>('[data-testid="posture-chip"]');
    expect(chip?.textContent?.trim()).toMatch(/^Long · 1 position$/);
    expect(chip?.classList.contains('long')).toBe(true);

    const pill = el.querySelector<HTMLElement>(
      '[data-testid="positions-list"] .position-pill',
    );
    expect(pill?.classList.contains('long')).toBe(true);
    expect(pill?.textContent ?? '').toContain('SPY');
    expect(pill?.textContent ?? '').toContain('100');
  });

  it('renders short posture and tone when qty is negative', () => {
    const el = render({
      broker: makeBroker({ owned_positions: { SPY: -50 } }),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const chip = el.querySelector<HTMLElement>('[data-testid="posture-chip"]');
    expect(chip?.classList.contains('short')).toBe(true);
  });

  it('renders mixed posture when long and short positions coexist', () => {
    const el = render({
      broker: makeBroker({ owned_positions: { SPY: 100, QQQ: -50 } }),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const chip = el.querySelector<HTMLElement>('[data-testid="posture-chip"]');
    expect(chip?.textContent?.trim()).toMatch(/^Mixed · 2 positions$/);
    expect(chip?.classList.contains('mixed')).toBe(true);
  });

  it('surfaces the orders_cap gate detail verbatim (User Story #18 — no prose re-parsing)', () => {
    const detail = '2 of 3 daily orders used';
    const el = render({
      broker: makeBroker(),
      readiness: makeReadiness([
        {
          name: 'orders_cap',
          status: 'pass',
          severity: 'hard',
          detail,
        },
      ]),
      sizing: makeSizing(),
    });

    const row = el.querySelector<HTMLElement>('[data-testid="orders-cap-row"]');
    expect(row?.textContent ?? '').toContain(detail);
    expect(row?.classList.contains('ok')).toBe(true);
  });

  it('is honest when the engine has not emitted an orders_cap gate', () => {
    const el = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const row = el.querySelector<HTMLElement>('[data-testid="orders-cap-row"]');
    expect(row?.textContent ?? '').toContain('not reported by the engine');
    expect(row?.classList.contains('unknown')).toBe(true);
  });

  it('shows pending order count from the broker slice', () => {
    const el = render({
      broker: makeBroker({ pending_order_count: 4 }),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const cell = el.querySelector<HTMLElement>('[data-testid="pending-count"]');
    expect(cell?.textContent?.trim()).toBe('4');
  });

  it('shows the order-namespace caption when present', () => {
    const el = render({
      broker: makeBroker({ bot_order_namespace: 'spy_15m_breakout' }),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const caption = el.querySelector<HTMLElement>(
      '[data-testid="namespace-caption"]',
    );
    expect(caption?.textContent ?? '').toContain('spy_15m_breakout');
  });

  it('degrades to "Position posture unknown" when broker is null', () => {
    const el = render({
      broker: null,
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const chip = el.querySelector<HTMLElement>('[data-testid="posture-chip"]');
    expect(chip?.textContent?.trim()).toBe('Position posture unknown');
    expect(chip?.classList.contains('unknown')).toBe(true);
  });

  it('filters out zero-qty entries from owned_positions', () => {
    const el = render({
      broker: makeBroker({ owned_positions: { SPY: 100, QQQ: 0 } }),
      readiness: makeReadiness(),
      sizing: makeSizing(),
    });

    const pills = el.querySelectorAll('[data-testid="positions-list"] .position-pill');
    expect(pills.length).toBe(1);
    expect(pills[0].textContent ?? '').toContain('SPY');
  });

  it('labels a pre-policy run honestly', () => {
    const el = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing({ preset: null }),
    });

    expect(el.textContent ?? '').toContain('Pre-policy run (legacy ledger)');
  });

  // PRD #607 / Slice 5 (#612) — server-authored posture/pending/verdict.

  it.each([
    ['FLAT', 'flat'],
    ['LONG', 'long'],
    ['SHORT', 'short'],
    ['MIXED', 'mixed'],
    ['UNKNOWN', 'unknown'],
  ] as const)('renders posture chip from server-authored %s', (server, ui) => {
    const el = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
      currentRisk: { ...DEFAULT_RISK, posture: server },
    });
    expect(
      el.querySelector('[data-testid="posture-chip"]')?.getAttribute('data-posture'),
    ).toBe(ui);
  });

  it('renders [unknown] badge with operator-language tooltip when posture is UNKNOWN', () => {
    const el = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
      currentRisk: { ...DEFAULT_RISK, posture: 'UNKNOWN' },
    });
    const badge = el.querySelector('[data-testid="posture-unknown-badge"]');
    expect(badge).not.toBeNull();
    expect(badge?.getAttribute('title')?.toLowerCase()).toContain('broker state');
  });

  it('renders — for null pending and the actual count when 0 or higher', () => {
    const nullEl = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
      currentRisk: { ...DEFAULT_RISK, pending_order_count: null },
    });
    expect(
      nullEl.querySelector('[data-testid="pending-count"]')?.textContent?.trim(),
    ).toBe('—');

    const zeroEl = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
      currentRisk: { ...DEFAULT_RISK, pending_order_count: 0 },
    });
    expect(
      zeroEl.querySelector('[data-testid="pending-count"]')?.textContent?.trim(),
    ).toBe('0');

    const fiveEl = render({
      broker: makeBroker(),
      readiness: makeReadiness(),
      sizing: makeSizing(),
      currentRisk: { ...DEFAULT_RISK, pending_order_count: 5 },
    });
    expect(
      fiveEl.querySelector('[data-testid="pending-count"]')?.textContent?.trim(),
    ).toBe('5');
  });
});
