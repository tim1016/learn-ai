import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type { ClerkTransactionHistoryResponse } from '../../../../../api/clerk-transaction-history.types';
import type { AccountSafetySnapshotState } from '../../../account-safety/account-safety-snapshot.store';
import { makeAccountSafetySnapshot } from '../../../../../../testing/account-safety-snapshot-fixtures';
import { BrokerService } from '../../../../../services/broker.service';
import { BotCustodyRibbonComponent } from './bot-custody-ribbon.component';

function evidencePage(recordCount: number, intentId: string): ClerkTransactionHistoryResponse {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    feed_state: 'live',
    feed_headline: 'Projected custody evidence is current.',
    feed_detail: 'The Clerk receipt projection is available.',
    high_water_journal_seq: 22,
    lag_records: 0,
    lag_is_lower_bound: false,
    custody_summary: {
      record_count: recordCount,
      a0_custody_accepted_count: 1,
      a1_broker_write_started_count: 1,
      a2_broker_known_count: 1,
      a3_economic_terminal_count: 1,
      uncertain_count: recordCount - 4,
    },
    rows: [{
      transaction_id: `ctxn-${intentId}`,
      broker: 'ibkr',
      account_id: 'DU1234567',
      journal_seq: 22,
      recorded_at_ms: 1_780_000_000_000,
      transaction_kind: 'strategy_order',
      strategy_instance_id: 'bot-a',
      run_id: 'run-a',
      intent_id: intentId,
      order_ref: `order/${intentId}`,
      order_id: null,
      perm_id: null,
      exec_id: null,
      native_order_id: null,
      native_execution_id: null,
      lifecycle_state: 'filled',
      commission_status: 'reported',
      fee: null,
      event_count: 2,
    }],
    next_cursor: null,
  };
}

function reconcileState(): AccountSafetySnapshotState {
  return {
    state: 'fresh',
    snapshot: makeAccountSafetySnapshot({ verdict: 'RECONCILING' }),
  };
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('BotCustodyRibbonComponent', () => {
  it('renders server-folded bounded evidence without implying an account verdict', async () => {
    const broker = {
      accountTransactions: vi.fn().mockResolvedValue(evidencePage(5, 'intent/opaque')),
      accountTransaction: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
    const fixture = TestBed.createComponent(BotCustodyRibbonComponent);
    fixture.componentRef.setInput('accountId', 'DU1234567');
    fixture.componentRef.setInput('strategyInstanceId', 'bot-a');
    fixture.componentRef.setInput('accountSafetyState', reconcileState());
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(broker.accountTransactions).toHaveBeenCalledWith(
      'DU1234567', null, 50, { strategyInstanceId: 'bot-a' },
    );
    expect(fixture.nativeElement.textContent).toContain('A0');
    expect(fixture.nativeElement.textContent).toContain('5 projected records');
    expect(fixture.nativeElement.textContent).toContain('Account reconciliation is still pending');
    expect(fixture.nativeElement.textContent).toContain('not account-wide truth');
    expect(fixture.nativeElement.textContent).toContain('intent/opaque');
  });

  it('discards a late response from a prior namespace request', async () => {
    const first = deferred<ClerkTransactionHistoryResponse>();
    const second = deferred<ClerkTransactionHistoryResponse>();
    const broker = {
      accountTransactions: vi.fn()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise),
      accountTransaction: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
    const fixture = TestBed.createComponent(BotCustodyRibbonComponent);
    fixture.componentRef.setInput('accountId', 'DU1234567');
    fixture.componentRef.setInput('strategyInstanceId', 'bot-a');
    fixture.detectChanges();
    fixture.componentRef.setInput('strategyInstanceId', 'bot-b');
    fixture.detectChanges();

    second.resolve(evidencePage(5, 'intent/new'));
    await fixture.whenStable();
    fixture.detectChanges();
    first.resolve(evidencePage(5, 'intent/stale'));
    await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('intent/new');
    expect(fixture.nativeElement.textContent).not.toContain('intent/stale');
  });

  it('clears prior-namespace evidence while the new namespace request is pending', async () => {
    const next = deferred<ClerkTransactionHistoryResponse>();
    const broker = {
      accountTransactions: vi.fn()
        .mockResolvedValueOnce(evidencePage(5, 'intent/old-namespace'))
        .mockReturnValueOnce(next.promise),
      accountTransaction: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
    const fixture = TestBed.createComponent(BotCustodyRibbonComponent);
    fixture.componentRef.setInput('accountId', 'DU1234567');
    fixture.componentRef.setInput('strategyInstanceId', 'bot-a');
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('intent/old-namespace');

    fixture.componentRef.setInput('strategyInstanceId', 'bot-b');
    fixture.detectChanges();
    await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('intent/old-namespace');
    expect(fixture.nativeElement.textContent).toContain('Loading the saved custody projection');
  });
});
