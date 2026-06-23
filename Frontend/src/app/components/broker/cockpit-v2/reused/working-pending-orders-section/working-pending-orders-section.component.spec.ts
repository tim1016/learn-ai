import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import { WorkingPendingOrdersSectionComponent } from './working-pending-orders-section.component';
import type { BrokerActivityRow } from '../broker-activity-table/broker-activity.types';

function row(overrides: Partial<BrokerActivityRow> = {}): BrokerActivityRow {
  return {
    seq: 1,
    ts_ms: 1_700_000_000_000,
    exec_id: null,
    perm_id: null,
    order_ref: 'learn-ai/sid/v1/intent-1',
    symbol: 'SPY',
    side: 'BUY',
    quantity: 10,
    price: null,
    commission: null,
    net_amount: null,
    order_type: 'MKT',
    exec_ts_ms: null,
    verdict: 'engine_only_pending',
    template_key: 'pending_v1',
    template_version: 1,
    headline: 'Awaiting broker ack',
    narrative: 'Engine emitted intent; no broker ack yet.',
    reason_codes: ['pending_acknowledgement'],
    engine_overlay: {
      intent_id: 'intent-1',
      mutation_attempt_id: null,
      requested_qty: 10,
      requested_price: null,
      sizing_provenance: null,
      lag_breakdown: {
        intent_to_dispatch_ms: null,
        dispatch_to_ack_ms: null,
        ack_to_exec_ms: null,
        exec_to_observed_ms: null,
        intent_to_exec_ms: null,
      },
    },
    divergence_facts: null,
    ...overrides,
  };
}

function render(rows: BrokerActivityRow[]): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(WorkingPendingOrdersSectionComponent);
  fixture.componentRef.setInput('rows', rows);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('WorkingPendingOrdersSectionComponent', () => {
  it('renders nothing when no engine_only_pending rows are present', () => {
    const el = render([
      row({ seq: 1, verdict: 'expected', symbol: 'AAPL' }),
    ]);
    expect(el.querySelector('[data-testid="working-pending-orders"]')).toBeNull();
  });

  it('renders only engine_only_pending rows (filter is layout, not derivation)', () => {
    // Each row needs a distinct ``order_ref`` so slice-7's supersede
    // filter doesn't hide the SPY/TSLA pending rows when an unrelated
    // ``expected`` row (AAPL here) shares the default order_ref.
    const el = render([
      row({
        seq: 1,
        symbol: 'SPY',
        verdict: 'engine_only_pending',
        order_ref: 'learn-ai/sid/v1/intent-spy',
      }),
      row({
        seq: 2,
        symbol: 'AAPL',
        verdict: 'expected',
        order_ref: 'learn-ai/sid/v1/intent-aapl',
      }),
      row({
        seq: 3,
        symbol: 'TSLA',
        verdict: 'engine_only_pending',
        order_ref: 'learn-ai/sid/v1/intent-tsla',
      }),
    ]);
    const panel = el.querySelector('[data-testid="working-pending-orders"]');
    expect(panel).not.toBeNull();
    const text = panel?.textContent ?? '';
    expect(text).toContain('SPY');
    expect(text).toContain('TSLA');
    expect(text).not.toContain('AAPL');
  });

  it('renders the backend-authored headline VERBATIM in the status column', () => {
    const el = render([
      row({ seq: 7, headline: 'Awaiting broker ack (queued)' }),
    ]);
    const text = el.textContent ?? '';
    expect(text).toContain('Awaiting broker ack (queued)');
  });

  it('renders the intent_id and quantity from the row', () => {
    const el = render([
      row({
        seq: 8,
        quantity: 25,
        engine_overlay: {
          intent_id: 'intent-abc',
          mutation_attempt_id: null,
          requested_qty: 25,
          requested_price: null,
          sizing_provenance: null,
          lag_breakdown: {
            intent_to_dispatch_ms: null,
            dispatch_to_ack_ms: null,
            ack_to_exec_ms: null,
            exec_to_observed_ms: null,
            intent_to_exec_ms: null,
          },
        },
      }),
    ]);
    const text = el.textContent ?? '';
    expect(text).toContain('intent-abc');
    expect(text).toContain('25');
  });

  it('renders — for the intent id when engine_overlay is missing', () => {
    const el = render([row({ seq: 9, engine_overlay: null })]);
    expect(el.textContent ?? '').toContain('—');
  });

  // Slice 7 (handoff gap #1): once the publisher's pending row is
  // superseded by a broker fill / cancel row carrying the same
  // ``order_ref``, the panel must hide the pending row so the
  // operator sees broker-resolved state without a reload.
  it('hides a pending row once a later row with the same order_ref arrives', () => {
    const el = render([
      row({
        seq: 1,
        symbol: 'SPY',
        order_ref: 'learn-ai/sid/v1/intent-shared',
        verdict: 'engine_only_pending',
      }),
      row({
        seq: 2,
        symbol: 'SPY',
        order_ref: 'learn-ai/sid/v1/intent-shared',
        verdict: 'expected',
        reason_codes: ['cancellation'],
      }),
    ]);
    expect(el.querySelector('[data-testid="working-pending-orders"]')).toBeNull();
  });

  it('keeps a pending row visible when no other row shares its order_ref', () => {
    const el = render([
      row({
        seq: 1,
        symbol: 'SPY',
        order_ref: 'learn-ai/sid/v1/intent-still-pending',
        verdict: 'engine_only_pending',
      }),
      row({
        seq: 2,
        symbol: 'AAPL',
        order_ref: 'learn-ai/sid/v1/intent-unrelated',
        verdict: 'expected',
        reason_codes: ['cancellation'],
      }),
    ]);
    const panel = el.querySelector('[data-testid="working-pending-orders"]');
    expect(panel).not.toBeNull();
    expect(panel?.textContent ?? '').toContain('SPY');
  });
});
