import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import { BrokerActivityRowDetailComponent } from './broker-activity-row-detail.component';
import type { BrokerActivityRow } from '../broker-activity-table/broker-activity.types';

function row(overrides: Partial<BrokerActivityRow> = {}): BrokerActivityRow {
  return {
    seq: 42,
    ts_ms: 1_700_000_000_000,
    exec_id: 'exec-42',
    perm_id: 9001,
    order_ref: 'learn-ai/sid/v1/intent-42',
    symbol: 'SPY',
    side: 'BUY',
    quantity: 10,
    price: 420.5,
    commission: 1.0,
    net_amount: -4206.0,
    order_type: 'MKT',
    exec_ts_ms: 1_700_000_000_500,
    verdict: 'expected',
    template_key: 'normal_fill_v1',
    template_version: 1,
    headline: 'BUY 10 SPY @ $420.50',
    narrative: 'Filled as intended.',
    reason_codes: ['normal_fill'],
    engine_overlay: null,
    divergence_facts: null,
    ...overrides,
  };
}

function render(props: BrokerActivityRow): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(BrokerActivityRowDetailComponent);
  fixture.componentRef.setInput('row', props);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerActivityRowDetailComponent', () => {
  it('renders the backend-authored narrative VERBATIM (truthfulness contract)', () => {
    const el = render(row({ narrative: 'Filled as intended — no caveats.' }));
    const n = el.querySelector('[data-testid="broker-activity-narrative"]');
    expect(n?.textContent ?? '').toBe('Filled as intended — no caveats.');
  });

  it('renders template_key + template_version so operators can trace authoring', () => {
    const el = render(row({ template_key: 'reconnect_recovery', template_version: 3 }));
    expect(el.textContent ?? '').toContain('reconnect_recovery');
    expect(el.textContent ?? '').toContain('v3');
  });

  it('hides the engine-overlay panel when the row has no engine match', () => {
    const el = render(row({ engine_overlay: null }));
    expect(el.querySelector('[data-testid="broker-activity-engine-overlay"]')).toBeNull();
  });

  it('renders engine overlay fields + lag breakdown when present', () => {
    const el = render(
      row({
        engine_overlay: {
          intent_id: 'intent-xyz',
          mutation_attempt_id: 'mut-7',
          requested_qty: 11,
          requested_price: 420.0,
          sizing_provenance: null,
          lag_breakdown: {
            intent_to_dispatch_ms: 100,
            dispatch_to_ack_ms: 200,
            ack_to_exec_ms: 50,
            exec_to_observed_ms: 25,
            intent_to_exec_ms: 375,
          },
        },
      }),
    );
    const overlay = el.querySelector('[data-testid="broker-activity-engine-overlay"]');
    expect(overlay).not.toBeNull();
    const text = overlay?.textContent ?? '';
    expect(text).toContain('intent-xyz');
    expect(text).toContain('mut-7');
    expect(text).toContain('100 ms');
    expect(text).toContain('200 ms');
    expect(text).toContain('375 ms'); // intent_to_exec total
  });

  it('renders sizing provenance fields under the engine overlay', () => {
    const el = render(
      row({
        engine_overlay: {
          intent_id: 'i',
          mutation_attempt_id: null,
          requested_qty: null,
          requested_price: null,
          sizing_provenance: {
            policy: 'SetHoldings',
            requested_qty: 10,
            reference_price_decimal_str: '420.50',
            provenance: 'reference_native',
            surface: 'live_engine',
            skip_reason: null,
          },
          lag_breakdown: {
            intent_to_dispatch_ms: null,
            dispatch_to_ack_ms: null,
            ack_to_exec_ms: null,
            exec_to_observed_ms: null,
            intent_to_exec_ms: null,
          },
        },
      }),
    );
    const sp = el.querySelector('[data-testid="broker-activity-sizing-provenance"]');
    expect(sp).not.toBeNull();
    const text = sp?.textContent ?? '';
    expect(text).toContain('SetHoldings');
    expect(text).toContain('reference_native');
    expect(text).toContain('420.50');
  });

  it('hides the divergence-facts panel when the row has none', () => {
    const el = render(row({ divergence_facts: null }));
    expect(el.querySelector('[data-testid="broker-activity-divergence-facts"]')).toBeNull();
  });

  it('renders divergence facts including window_context as a sub-panel', () => {
    const el = render(
      row({
        verdict: 'unexpected',
        divergence_facts: {
          price_delta: 0.25,
          quantity_delta: 1,
          lag_total_ms: 1200,
          window_context: {
            reconnect_window_start_ms: 1_700_000_000_000,
            reason: 'late_fill',
          },
        },
      }),
    );
    const facts = el.querySelector('[data-testid="broker-activity-divergence-facts"]');
    expect(facts).not.toBeNull();
    const text = facts?.textContent ?? '';
    expect(text).toContain('1,200 ms');
    expect(text).toContain('reconnect_window_start_ms');
    expect(text).toContain('late_fill');
  });

  it('renders reason codes when present and hides the section when empty', () => {
    const elWith = render(row({ reason_codes: ['price_divergence', 'timing_caveat'] }));
    const withRC = elWith.querySelector('[data-testid="broker-activity-reason-codes"]');
    expect(withRC?.textContent ?? '').toContain('price_divergence');
    expect(withRC?.textContent ?? '').toContain('timing_caveat');

    const elWithout = render(row({ reason_codes: [] }));
    expect(
      elWithout.querySelector('[data-testid="broker-activity-reason-codes"]'),
    ).toBeNull();
  });
});
