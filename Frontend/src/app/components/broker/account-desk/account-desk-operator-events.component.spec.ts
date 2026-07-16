import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { AccountDeskEventsStore } from './account-desk-events-store.service';
import { AccountDeskOperatorEventsComponent } from './account-desk-operator-events.component';

function makeStore(overrides: Record<string, unknown> = {}) {
  return {
    operationRows: signal([{
      schema_version: 1 as const,
      event_id: 'DU1234567:5',
      seq: 5,
      kind: 'reconciliation' as const,
      occurred_at_ms: 1_780_000_000_000,
      trader_narration: null,
      operator_detail: 'Account reconciliation receipt recorded in the journal.',
      evidence_refs: [{ source: 'account_event_journal', ref: 'DU1234567:5', detail: null }],
    }]),
    operationsLoading: signal(false),
    operationsErrorMessage: signal<string | null>(null),
    operationsHasLastGood: signal(true),
    operationsShowingStaleLastGood: signal(false),
    nextBeforeSeq: signal<number | null>(4),
    operationKinds: signal<readonly string[]>([]),
    toggleOperationKind: vi.fn(),
    retry: vi.fn(),
    loadOlder: vi.fn(),
    ...overrides,
  };
}

describe('AccountDeskOperatorEventsComponent', () => {
  it('renders backend operator detail, local instants, opaque evidence, filters, and load older', async () => {
    const store = makeStore();
    await render(AccountDeskOperatorEventsComponent, {
      providers: [{ provide: AccountDeskEventsStore, useValue: store }],
    });

    expect(await screen.findByText('Account event timeline')).toBeTruthy();
    expect(screen.getByText('Account reconciliation receipt recorded in the journal.')).toBeTruthy();
    expect(screen.getByText('DU1234567:5')).toBeTruthy();
    expect(document.querySelector('[data-timestamp-mode="local"]')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Safety' }));
    fireEvent.click(screen.getByRole('button', { name: 'Load older' }));
    expect(store.toggleOperationKind).toHaveBeenCalledWith('safety');
    expect(store.loadOlder).toHaveBeenCalledOnce();
  });

  it('renders an honest operations error rather than empty history', async () => {
    const store = makeStore({
      operationRows: signal([]),
      operationsErrorMessage: signal('Account event history is unavailable.'),
      operationsHasLastGood: signal(false),
      nextBeforeSeq: signal(null),
    });
    await render(AccountDeskOperatorEventsComponent, {
      providers: [{ provide: AccountDeskEventsStore, useValue: store }],
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Account event history is unavailable.');
    expect(screen.queryByText(/No account journal/)).toBeNull();
  });
});
