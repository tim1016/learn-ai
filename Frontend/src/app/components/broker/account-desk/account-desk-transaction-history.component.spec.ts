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
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../fleet/resource-target';
import type { LaneFence } from '../../../fleet/lane-fence';

const TARGET = resourceTarget('alpaca', 'clerk-1', {
  accountId: 'PA1', bindingGeneration: 7, routingEpoch: 4,
});
const NEXT_TARGET = resourceTarget('alpaca', 'clerk-2', {
  accountId: 'PA2', bindingGeneration: 3, routingEpoch: 5,
});
const FENCE: LaneFence = { bindingGeneration: 7, routingEpoch: 4 };

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

    expect(accountTransaction).toHaveBeenCalledWith('clerk-1', 'PA1', 'ctxn-1');
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
      expect.objectContaining({
        broker: 'alpaca',
        clerkId: 'clerk-1',
        accountId: 'PA1',
        capability: 'custody_command',
      }),
      'alpaca-external-1',
      'operator@example.test',
    ));
    expect(load).toHaveBeenCalledWith('PA1');
  });

  /** #2106: `target` arrives as an `input()` sourced from the live fleet
   * directory. If the lane rebinds between when the row rendered and when
   * the operator opens its receipt, minting the acknowledgement command from
   * `target()` directly would silently carry the new (wrong) generation. The
   * frozen `fence` input, not `target()`'s live generation, must decide what
   * is sent. */
  it('sends the generation shown at render, not the one current when the receipt is opened', async () => {
    const acknowledgeExternalOrder = vi.fn().mockResolvedValue(undefined);
    const view = await renderHistory({
      load: vi.fn().mockResolvedValue(undefined),
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

    // The lane rebinds (a directory refresh) after the row rendered but
    // before the operator opens its receipt. `target()` now reports the new
    // generation; the fence frozen at render time, `fence`, does not move.
    view.fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clerk-1', {
      accountId: 'PA1', bindingGeneration: 99, routingEpoch: 55,
    }));
    view.fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: /view evidence for alpaca-external-1/i }));
    fireEvent.input(screen.getByRole('textbox', { name: 'Operator' }), {
      target: { value: 'operator@example.test' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge external order' }));

    await vi.waitFor(() => expect(acknowledgeExternalOrder).toHaveBeenCalledWith(
      expect.objectContaining({ bindingGeneration: 7, routingEpoch: 4 }),
      'alpaca-external-1',
      'operator@example.test',
    ));
  });

  /** #2106: a cold directory at render time freezes `fence` with a null
   * generation; `commandContextOf` sends no generation check at all for one.
   * Refuse rather than dispatch blind. */
  it('refuses to open a receipt when the lane had no known binding at render', async () => {
    const acknowledgeExternalOrder = vi.fn().mockResolvedValue(undefined);
    await renderHistory({
      load: vi.fn().mockResolvedValue(undefined),
      rows: signal([transaction({
        transaction_origin: 'external',
        order_ref: null,
        external_order_id: 'alpaca-external-1',
        lifecycle_state: 'review_required',
      })]),
    }, { acknowledgeExternalOrder }, false, vi.fn(), { bindingGeneration: null, routingEpoch: null });

    fireEvent.click(screen.getByRole('button', { name: /view evidence for alpaca-external-1/i }));

    expect(screen.queryByRole('button', { name: 'Acknowledge external order' })).toBeNull();
    expect(acknowledgeExternalOrder).not.toHaveBeenCalled();
    expect(await screen.findByText(/no known binding when the action was opened/i)).toBeTruthy();
  });

  /** #2106: `currentReceiptContext` (and the effect that resets an open
   * receipt when it changes) is keyed off `target()`'s live generation. A
   * lane rebind while a receipt is open would then read as "the desk
   * changed" and silently close the receipt out from under the operator,
   * even though the fenced identity backing the pending command has not
   * moved. It must key off the frozen fence instead. */
  it('keeps an open receipt through a lane rebind instead of silently closing it', async () => {
    const view = await renderHistory({
      load: vi.fn().mockResolvedValue(undefined),
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
    });

    fireEvent.click(screen.getByRole('button', { name: /view evidence for alpaca-external-1/i }));
    fireEvent.input(screen.getByRole('textbox', { name: 'Operator' }), {
      target: { value: 'operator@example.test' },
    });

    // The lane rebinds (a directory refresh) while the receipt stays open.
    view.fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clerk-1', {
      accountId: 'PA1', bindingGeneration: 99, routingEpoch: 55,
    }));
    view.fixture.detectChanges();

    expect((screen.getByRole('textbox', { name: 'Operator' }) as HTMLInputElement).value)
      .toBe('operator@example.test');
    expect(screen.getByRole('button', { name: 'Acknowledge external order' })).toBeTruthy();
  });

  it('does not refresh a new lane when an old acknowledgement finishes late', async () => {
    let release = (): void => undefined;
    const pending = new Promise<void>((resolve) => {
      release = resolve;
    });
    const acknowledgeExternalOrder = vi.fn(() => pending);
    const load = vi.fn().mockResolvedValue(undefined);
    const view = await renderHistory({
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
      target: { value: 'operator@example.test' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge external order' }));
    await vi.waitFor(() => expect(acknowledgeExternalOrder).toHaveBeenCalledTimes(1));

    view.fixture.componentRef.setInput('target', NEXT_TARGET);
    view.fixture.componentRef.setInput('accountId', 'PA2');
    view.fixture.detectChanges();
    release();
    await pending;
    await view.fixture.whenStable();

    expect(load).not.toHaveBeenCalledWith('PA1');
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
    await vi.waitFor(() => expect(getPortfolioHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ broker: 'alpaca', accountId: 'PA1' }), '30D',
    ));
    fireEvent.click(screen.getByRole('button', { name: '60D' }));
    await vi.waitFor(() => expect(getPortfolioHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({ broker: 'alpaca', accountId: 'PA1' }), '60D',
    ));
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
  fence: LaneFence | null = FENCE,
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
    setTarget: vi.fn(),
    retry: vi.fn(),
    load: vi.fn().mockResolvedValue(undefined),
    ...storeOverrides,
  };
  return render(AccountDeskTransactionHistoryComponent, {
    inputs: showScopeControl
      ? { accountId: 'PA1', target: TARGET, fence, showScopeControl: true }
      : { accountId: 'PA1', target: TARGET, fence, showScopeControl: false, fromMs: 1, toMs: 2 },
    providers: [
      provideFleetDirectory(),
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
