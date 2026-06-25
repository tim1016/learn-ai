import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { ActivityOrderRow } from '../bot-trade-chart-card/bot-trade-chart-card.types';
import { WorkingPendingOrdersSectionComponent } from './working-pending-orders-section.component';

function order(overrides: Partial<ActivityOrderRow> = {}): ActivityOrderRow {
  return {
    order_key: 'perm:1',
    symbol: 'SPY',
    side: 'BUY',
    quantity: 1,
    order_type: 'MKT',
    status: 'filled',
    group: 'resolved',
    submitted_ts_ms: 1_700_000_000_000,
    last_update_ts_ms: 1_700_000_060_000,
    filled_quantity: 1,
    avg_fill_price: 420.5,
    position_effect: 'Open long',
    replay_count: 1,
    evidence: [],
    ...overrides,
  };
}

function render(orders: ActivityOrderRow[]): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(WorkingPendingOrdersSectionComponent);
  fixture.componentRef.setInput('orders', orders);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('WorkingPendingOrdersSectionComponent', () => {
  it('renders nothing when the projection has no same-day orders', () => {
    const el = render([]);
    expect(el.querySelector('[data-testid="orders-today"]')).toBeNull();
  });

  it('renders active, engine-pending, and resolved groups from backend-authored group values', () => {
    const el = render([
      order({ order_key: 'active', group: 'active', status: 'submitted', symbol: 'AAPL' }),
      order({ order_key: 'pending', group: 'engine_pending', status: 'engine pending', symbol: 'SPY' }),
      order({ order_key: 'resolved', group: 'resolved', status: 'filled', symbol: 'TSLA' }),
    ]);

    const text = el.textContent ?? '';
    expect(text).toContain('ORDERS TODAY');
    expect(text).toContain('Active');
    expect(text).toContain('Engine pending');
    expect(text).toContain('Resolved');
    expect(text).toContain('AAPL');
    expect(text).toContain('SPY');
    expect(text).toContain('TSLA');
  });

  it('renders position effect and replay count from the projection', () => {
    const el = render([
      order({
        order_key: 'perm:replayed',
        side: 'SELL',
        status: 'filled',
        position_effect: 'Close long',
        replay_count: 3,
      }),
    ]);

    const text = el.textContent ?? '';
    expect(text).toContain('Close long');
    expect(text).toContain('seen 3x');
  });
});
