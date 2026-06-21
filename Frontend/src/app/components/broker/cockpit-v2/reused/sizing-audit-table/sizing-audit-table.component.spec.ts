import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { SizingAuditRow } from '../../../../../api/live-instances.types';
import { SizingAuditTableComponent } from './sizing-audit-table.component';

function row(overrides: Partial<SizingAuditRow> = {}): SizingAuditRow {
  return {
    ts_ms: 1_700_000_000_000,
    symbol: 'SPY',
    policy_kind: 'fraction',
    policy_value: '0.01',
    intended_qty: 5,
    reference_price: '420.10',
    sized_via: 'live_config',
    sizing_provenance_at_resolve_time: 'reference_native',
    skipped: false,
    skip_reason: null,
    ...overrides,
  };
}

function render(rows: SizingAuditRow[]): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(SizingAuditTableComponent);
  fixture.componentRef.setInput('rows', rows);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('SizingAuditTableComponent', () => {
  it('renders nothing when the row list is empty', () => {
    const el = render([]);
    expect(el.querySelector('[data-testid="sizing-audit-table"]')).toBeNull();
  });

  it('renders the table with the actual SizingAuditRow columns', () => {
    const el = render([row({ symbol: 'SPY', intended_qty: 7 })]);
    const tbody = el.querySelector('tbody');
    expect(tbody).not.toBeNull();
    expect(tbody?.textContent ?? '').toContain('SPY');
    expect(tbody?.textContent ?? '').toContain('7');
  });

  it.each([
    ['reference_native', 'reference native'],
    ['live_override', 'live override'],
    ['spec_default', 'spec default'],
    [null, 'unknown'],
  ] as const)(
    'maps provenance %s -> %s',
    (provenance, label) => {
      const el = render([
        row({ sizing_provenance_at_resolve_time: provenance ?? null }),
      ]);
      expect((el.textContent ?? '').toLowerCase()).toContain(label);
    },
  );

  it('renders Skipped: <reason> for skipped rows and Filled otherwise', () => {
    const el = render([
      row({ skipped: true, skip_reason: 'target_equals_current' }),
      row({ skipped: false }),
    ]);
    const text = el.textContent ?? '';
    expect(text).toContain('Skipped: target_equals_current');
    expect(text).toContain('Filled');
  });
});
