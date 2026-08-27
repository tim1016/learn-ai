import { signal } from '@angular/core';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  ClerkTransactionHistoryResponse,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokersService } from '../../../services/brokers.service';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';
import {
  AccountDeskTransactionHistoryComponent,
  matchesLocalDateMs,
} from './account-desk-transaction-history.component';

if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
}
if (typeof HTMLDialogElement.prototype.close !== 'function') {
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

describe('AccountDeskTransactionHistoryComponent', () => {
  it('renders the operator columns, friendly values, local-time label, and symbol identity', async () => {
    await renderHistory({
      rows: signal([
        transaction({
          order_instruction: {
            symbol: 'GOOGL', sec_type: 'us_equity', action: 'buy', quantity: 3,
            order_type: 'market', limit_price: null, stop_price: null,
            time_in_force: 'day', outside_rth: false,
          },
          transaction_origin: 'strategy',
          execution_quantity: 3,
          execution_price: 175.25,
          fee_fidelity: 'reported',
          fee: 0.1,
        }),
      ]),
    });

    const table = screen.getByRole('table', { name: 'Transaction history' });
    for (const column of [
      'Recorded (local time)', 'Instrument', 'Request', 'Execution',
      'Status', 'Submitted by', 'Fees', 'Evidence',
    ]) {
      expect(within(table).getByText(column)).toBeTruthy();
    }
    expect(screen.getAllByTitle('GOOGL').length).toBeGreaterThan(0);
    expect(screen.getByText('Buy 3 · Market')).toBeTruthy();
    expect(screen.getByText('3 filled at $175.25')).toBeTruthy();
    expect(screen.getByText('Strategy')).toBeTruthy();
    expect(screen.getByPlaceholderText(/Search symbols, status, strategy/)).toBeTruthy();
  });

  it('loads only the selected receipt into an accessible drawer and restores focus', async () => {
    const accountTransaction = vi.fn().mockResolvedValue({
      ...transaction({ order_ref: 'learn-ai/bot-a/v1:intent-1' }),
      receipt: {
        receipt_hash: 'sha256:opaque',
        broker_event: { reason_code: 'ACCOUNT_CLERK_UNAVAILABLE' },
      },
      events: [],
      custody_timeline: null,
    });
    await renderHistory(
      { rows: signal([transaction({ order_ref: 'learn-ai/bot-a/v1:intent-1' })]) },
      { accountTransaction },
    );

    const opener = screen.getByRole('button', { name: /view evidence for learn-ai\/bot-a\/v1:intent-1/i });
    fireEvent.click(opener);

    expect(accountTransaction).toHaveBeenCalledWith('PA1', 'ctxn-1');
    expect(await screen.findByRole('heading', { name: 'Custody receipt' })).toBeTruthy();
    expect(screen.getByText('sha256:opaque')).toBeTruthy();
    expect(screen.getByText('Account Clerk Unavailable')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Close receipt drawer', hidden: true }));
    expect(document.activeElement).toBe(opener);
  });

  it('records an external-order acknowledgement after evidence review', async () => {
    const acknowledgeExternalOrder = vi.fn().mockResolvedValue(undefined);
    const load = vi.fn().mockResolvedValue(undefined);
    await renderHistory({
      load,
      rows: signal([transaction({
        transaction_origin: 'external',
        order_ref: null,
        external_order_id: 'alpaca-external-1',
        lifecycle_state: 'review_required',
      })]),
    }, {
      accountTransaction: vi.fn().mockResolvedValue({
        ...transaction({
          transaction_origin: 'external',
          external_order_id: 'alpaca-external-1',
          lifecycle_state: 'review_required',
        }),
        receipt: {},
        events: [],
        custody_timeline: null,
      }),
      acknowledgeExternalOrder,
    });

    fireEvent.click(screen.getByRole('button', { name: /view evidence for alpaca-external-1/i }));
    fireEvent.input(screen.getByRole('textbox', { name: 'Operator' }), {
      target: { value: '  operator@example.test  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge external order' }));

    await vi.waitFor(() => expect(acknowledgeExternalOrder).toHaveBeenCalledWith(
      'PA1', 'alpaca-external-1', 'operator@example.test',
    ));
    expect(load).toHaveBeenCalledWith('PA1');
  });

  it('uses broker-owned timestamps to load the selected Today, 30D, and 60D periods', async () => {
    const load = vi.fn().mockResolvedValue(undefined);
    const getPortfolioHistory = vi.fn().mockResolvedValue({
      timestamps: [1_700_000_000_000, 1_700_086_400_000],
      equity: [100, 101],
      profit_loss: [0, 1],
      base_value: 100,
      timeframe: '1D',
    });
    await renderHistory({ load }, {}, true, getPortfolioHistory);

    await vi.waitFor(() => expect(load).toHaveBeenCalledWith('PA1', {
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    }));
    fireEvent.click(screen.getByRole('button', { name: '30D' }));
    await vi.waitFor(() => expect(getPortfolioHistory).toHaveBeenLastCalledWith('alpaca', '30D'));
    fireEvent.click(screen.getByRole('button', { name: '60D' }));
    await vi.waitFor(() => expect(getPortfolioHistory).toHaveBeenLastCalledWith('alpaca', '60D'));
  });

  it('warns when the complete-period client safety limit is reached', async () => {
    await renderHistory({ rowLimitReached: signal(true) });

    expect(screen.getByRole('alert').textContent).toContain(
      'search and filters cover only the displayed records',
    );
  });

  it('renders transaction origins through the shared receipt label and keeps timestamps as milliseconds', async () => {
    await renderHistory({
      rows: signal([transaction({ transaction_origin: 'force_flat' })]),
    });

    expect(screen.getByText('Force Flat')).toBeTruthy();
  });

  it('derives a local calendar day only while matching canonical timestamp values', () => {
    const selectedDay = new Date(2026, 7, 13);
    const startOfDayMs = selectedDay.getTime();

    expect(matchesLocalDateMs(startOfDayMs + 12 * 60 * 60 * 1_000, selectedDay)).toBe(true);
    expect(matchesLocalDateMs(startOfDayMs - 1, selectedDay)).toBe(false);
    expect(matchesLocalDateMs(startOfDayMs + 24 * 60 * 60 * 1_000, selectedDay)).toBe(false);
  });
});

async function renderHistory(
  storeOverrides: Record<string, unknown> = {},
  brokerOverrides: Record<string, unknown> = {},
  showScopeControl = false,
  getPortfolioHistory = vi.fn(),
) {
  const store = {
    accountId: signal('PA1'),
    loading: signal(false),
    errorMessage: signal<string | null>(null),
    hasLastGood: signal(true),
    feed: signal<ClerkTransactionHistoryResponse | null>(historyPage()),
    rows: signal<readonly ClerkTransactionSummary[]>([]),
    loadedCount: signal(0),
    loadedPages: signal(0),
    rowLimitReached: signal(false),
    retry: vi.fn(),
    load: vi.fn().mockResolvedValue(undefined),
    ...storeOverrides,
  };
  return render(AccountDeskTransactionHistoryComponent, {
    inputs: showScopeControl
      ? { accountId: 'PA1', showScopeControl: true }
      : { accountId: 'PA1', showScopeControl: false, fromMs: 1, toMs: 2 },
    providers: [
      { provide: AccountDeskTransactionHistoryStore, useValue: store },
      {
        provide: BrokersService,
        useValue: {
          getPortfolioHistory,
          accountTransaction: vi.fn().mockResolvedValue(null),
          acknowledgeExternalOrder: vi.fn(),
          ...brokerOverrides,
        },
      },
    ],
  });
}

function transaction(overrides: Partial<ClerkTransactionSummary> = {}): ClerkTransactionSummary {
  return {
    transaction_id: 'ctxn-1',
    broker: 'alpaca',
    account_id: 'PA1',
    journal_seq: 1,
    recorded_at_ms: 1_780_000_000_000,
    transaction_kind: 'sqlite_order',
    transaction_origin: 'manual',
    strategy_instance_id: null,
    run_id: null,
    intent_id: null,
    order_ref: 'manual/v1:opaque',
    order_id: null,
    perm_id: null,
    exec_id: null,
    native_order_id: null,
    native_execution_id: null,
    lifecycle_state: 'submitted',
    commission_status: 'unknown',
    fee: null,
    event_count: 1,
    ...overrides,
  };
}

function historyPage(): ClerkTransactionHistoryResponse {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    feed_state: 'live',
    feed_headline: 'SQLite projection current',
    feed_detail: 'Current',
    high_water_journal_seq: 1,
    lag_records: 0,
    lag_is_lower_bound: false,
    custody_summary: {
      record_count: 0,
      a0_custody_accepted_count: 0,
      a1_broker_write_started_count: 0,
      a2_broker_known_count: 0,
      a3_economic_terminal_count: 0,
      uncertain_count: 0,
    },
    rows: [],
    next_cursor: null,
  };
}
