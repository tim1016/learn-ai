import { HttpErrorResponse } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { convertToParamMap, ActivatedRoute } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type {
  BrokerAccountSnapshot,
  CustodyDiagnosis,
} from '../../../api/alpaca.types';
import { BrokerService } from '../../../services/broker.service';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskComponent } from './alpaca-desk.component';
import { AlpacaOrderEntryComponent } from './alpaca-order-entry.component';

/**
 * `AlpacaOrderEntryComponent`'s real Signal Forms tree is covered by its own
 * dedicated spec. Driving it through this desk-level test as well pins two
 * independent signal graphs (this form's teardown on submit, and the account
 * history store's reactive reload) settling in jsdom at once, which triggers
 * an unrelated Signal Forms teardown crash outside this test's scope — see
 * `alpaca-order-entry.component.spec.ts` for the form's own submit coverage.
 * This stub emits the same `submissionFinished` output on demand so the
 * desk's refresh wiring is still exercised end to end.
 */
@Component({
  selector: 'app-alpaca-order-entry',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<button type="button" (click)="submissionFinished.emit()">Confirm & submit</button>`,
})
class FakeOrderEntryComponent {
  readonly initialSymbol = input('');
  readonly expectedAccountId = input.required<string>();
  readonly submissionFinished = output();
}

function transactionHistory(rows: readonly object[] = []) {
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
    rows,
    next_cursor: null,
  };
}

function submittedSpyTransaction() {
  return {
    transaction_id: 'sqlite-spy', broker: 'alpaca', account_id: 'PA1', journal_seq: 1,
    recorded_at_ms: 1_700_000_000_000, transaction_kind: 'strategy_execution',
    transaction_origin: 'strategy', strategy_instance_id: 'bot-spy', run_id: 'run-spy',
    intent_id: 'intent-spy', order_ref: 'learn-ai/bot-spy/v1:intent-spy', order_id: null,
    perm_id: null, exec_id: null, native_order_id: 'broker-order-spy', native_execution_id: 'exec-spy',
    lifecycle_state: 'submitted', commission_status: 'unknown', fee: null,
    execution_quantity: null, execution_price: null, fee_fidelity: 'not_reported', external_order_id: null, event_count: 1,
    order_instruction: {
      symbol: 'SPY', sec_type: 'us_equity', action: 'buy', quantity: 2,
      order_type: 'market', limit_price: null, time_in_force: 'day', outside_rth: false,
    },
  };
}

function inSyncDiagnosis(): CustodyDiagnosis {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    in_sync: true,
    observed_at_ms: 1,
    snapshot_version: 'v1',
    resolution_posture: 'paper',
    resolvable: false,
    blocked_reason: null,
    divergences: [],
    resolution_plan: [],
  };
}

function accountSnapshot(accountId = 'PA1'): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: accountId,
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 1_000,
    equity: 1_000,
    buying_power: 2_000,
    portfolio_value: 1_000,
    long_market_value: 0,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: null,
    observed_at_ms: 1,
  };
}

function brokerService(overrides: Partial<BrokersService> = {}) {
  return {
    getAccount: vi.fn().mockResolvedValue(accountSnapshot()),
    listPositions: vi.fn().mockResolvedValue([]),
    listOrders: vi.fn(),
    submitOrder: vi.fn(),
    getClerkStatus: vi.fn().mockResolvedValue({
      broker: 'alpaca',
      account_id: 'PA1',
      hold: { active: false, reason_code: null, reason: null, since_ms: null },
      latest_reconciliation: null,
      outstanding_intents: 0,
      observed_at_ms: 1,
    }),
    getCustodyDiagnosis: vi.fn().mockResolvedValue(inSyncDiagnosis()),
    getSqliteClerkProjection: vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    ),
    ...overrides,
  };
}

/** The account-desk transaction history child reads through `BrokerService`. */
function accountHistoryService(overrides: Partial<BrokerService> = {}) {
  return {
    accountTransactions: vi.fn().mockResolvedValue(transactionHistory()),
    accountTransaction: vi.fn(),
    ...overrides,
  };
}

describe('AlpacaDeskComponent', () => {
  it('renders the Alpaca desk heading and new-order action', async () => {
    await render(AlpacaDeskComponent, {
      providers: [
        { provide: BrokersService, useValue: brokerService() },
        { provide: BrokerService, useValue: accountHistoryService() },
      ],
    });

    expect(screen.getByRole('heading', { name: /Alpaca/i })).toBeTruthy();
    expect(screen.getByText('Broker desk')).toBeTruthy();
    expect(await screen.findByRole('button', { name: 'Create a new Alpaca order' })).toBeTruthy();
  });

  it('fails closed without a proven legacy authority and hides manual order entry', async () => {
    await render(AlpacaDeskComponent, {
      providers: [
        {
          provide: BrokersService,
          useValue: brokerService({
            getSqliteClerkProjection: vi.fn().mockRejectedValue(
              new HttpErrorResponse({ status: 503 }),
            ),
          }),
        },
        { provide: BrokerService, useValue: accountHistoryService() },
      ],
    });

    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Create a new Alpaca order' })).toBeNull();
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('opens an account-scoped order ticket with the routed symbol prefilled', async () => {
    const queryParamMap = convertToParamMap({
      order: 'new',
      accountId: 'PA3KWXU1C4C3',
      symbol: 'nvda',
    });

    await render(AlpacaDeskComponent, {
      providers: [
        {
          provide: BrokersService,
          useValue: brokerService({
            getAccount: vi.fn().mockResolvedValue(accountSnapshot('PA3KWXU1C4C3')),
          }),
        },
        { provide: BrokerService, useValue: accountHistoryService() },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: { queryParamMap },
            queryParamMap: of(queryParamMap),
          },
        },
      ],
    });

    expect(await screen.findByText('PA3KWXU1C4C3 · Equities')).toBeTruthy();
    const symbolInput = await screen.findByLabelText('Leg 1 symbol');
    if (!(symbolInput instanceof HTMLInputElement)) {
      throw new Error('Expected the routed symbol field to be an input.');
    }
    expect(symbolInput.value).toBe('NVDA');
  });

  it('refuses a routed ticket when the connected account differs', async () => {
    const queryParamMap = convertToParamMap({
      order: 'new',
      accountId: 'PA-STALE',
      symbol: 'NVDA',
    });

    await render(AlpacaDeskComponent, {
      providers: [
        { provide: BrokersService, useValue: brokerService() },
        { provide: BrokerService, useValue: accountHistoryService() },
        {
          provide: ActivatedRoute,
          useValue: {
            snapshot: { queryParamMap },
            queryParamMap: of(queryParamMap),
          },
        },
      ],
    });

    expect((await screen.findByRole('alert')).textContent).toContain(
      'The order link targets account PA-STALE, but Alpaca is connected to PA1. No ticket was opened.',
    );
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('refreshes transaction history after a successful submission', async () => {
    const accountTransactions = vi
      .fn()
      .mockResolvedValueOnce(transactionHistory())
      .mockResolvedValueOnce(transactionHistory([submittedSpyTransaction()]));
    const listOrders = vi.fn();

    TestBed.overrideComponent(AlpacaDeskComponent, {
      remove: { imports: [AlpacaOrderEntryComponent] },
      add: { imports: [FakeOrderEntryComponent] },
    });
    await render(AlpacaDeskComponent, {
      providers: [
        { provide: BrokersService, useValue: brokerService({ listOrders }) },
        { provide: BrokerService, useValue: accountHistoryService({ accountTransactions }) },
      ],
    });

    await screen.findByText('No projected transactions match this account and filter set.');
    fireEvent.click(await screen.findByRole('button', { name: 'Create a new Alpaca order' }));
    fireEvent.click(await screen.findByRole('button', { name: /Confirm & submit/i }));

    await waitFor(() => expect(accountTransactions).toHaveBeenCalledTimes(2));
    expect(listOrders).not.toHaveBeenCalled();
    expect(screen.getByText('Placed by bot-spy')).toBeTruthy();
    expect(screen.getByText(/SPY/)).toBeTruthy();
  });
});
