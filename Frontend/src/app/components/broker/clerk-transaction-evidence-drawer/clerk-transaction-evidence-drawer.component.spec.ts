import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type {
  ClerkTransactionDetail,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokerService } from '../../../services/broker.service';
import { ClerkTransactionEvidenceDrawerComponent } from './clerk-transaction-evidence-drawer.component';

function summary(accountId: string, transactionId: string): ClerkTransactionSummary {
  return {
    transaction_id: transactionId,
    broker: 'ibkr',
    account_id: accountId,
    journal_seq: 4,
    recorded_at_ms: 1_780_000_000_000,
    transaction_kind: 'strategy_submission',
    transaction_origin: 'strategy',
    strategy_instance_id: 'bot-a',
    run_id: 'run-a',
    intent_id: `intent/${transactionId}`,
    order_ref: `order/${transactionId}`,
    order_id: null,
    perm_id: null,
    exec_id: null,
    native_order_id: null,
    native_execution_id: null,
    lifecycle_state: 'submitted',
    commission_status: 'unknown',
    fee: null,
    event_count: 1,
  };
}

function detail(
  accountId: string,
  transactionId: string,
  receipt: Record<string, unknown>,
  events: ClerkTransactionDetail['events'] = [],
): ClerkTransactionDetail {
  const { event_count: _, ...transaction } = summary(accountId, transactionId);
  return { ...transaction, receipt, events, custody_timeline: null };
}

function deferred<T>(): { readonly promise: Promise<T>; readonly resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('ClerkTransactionEvidenceDrawerComponent', () => {
  it('loads a distinct receipt when opaque account and transaction identifiers contain a colon', async () => {
    const first = deferred<ClerkTransactionDetail>();
    const second = deferred<ClerkTransactionDetail>();
    const broker = {
      accountTransaction: vi.fn()
        .mockReturnValueOnce(first.promise)
        .mockReturnValueOnce(second.promise),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
    const fixture = TestBed.createComponent(ClerkTransactionEvidenceDrawerComponent);
    fixture.componentRef.setInput('accountId', 'acct');
    fixture.componentRef.setInput('transaction', summary('acct', 'a:b'));
    fixture.detectChanges();
    await Promise.resolve();

    fixture.componentRef.setInput('accountId', 'acct:a');
    fixture.componentRef.setInput('transaction', summary('acct:a', 'b'));
    fixture.detectChanges();
    await Promise.resolve();

    expect(broker.accountTransaction).toHaveBeenNthCalledWith(1, 'acct', 'a:b');
    expect(broker.accountTransaction).toHaveBeenNthCalledWith(2, 'acct:a', 'b');

    second.resolve(detail('acct:a', 'b', { receipt_hash: 'current-evidence' }));
    await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('current-evidence');
    first.resolve(detail('acct', 'a:b', { receipt_hash: 'stale-evidence' }));
    await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).not.toContain('stale-evidence');
  });

  it('humanizes event codes while preserving opaque values and every recorded observed epoch', async () => {
    const broker = {
      accountTransaction: vi.fn().mockResolvedValue(detail(
        'DU1234567',
        'ctxn_1',
        { origin_epoch: { clerk_boot_id: 'boot-a', epoch_seq: 1 } },
        [{
          event_id: 'event/opaque',
          broker: 'ibkr',
          event_kind: 'error',
          journal_seq: 5,
          recorded_at_ms: 1_780_000_000_001,
          callback_identity: 'callback/opaque',
          lifecycle_state: 'error',
          native_order_id: null,
          native_execution_id: null,
          commission_status: 'unknown',
          fee: null,
          receipt: {
            reason_code: 'NO_LIVE_BINDING',
            source: 'broker.connection',
            evidence_ref: 'event/opaque',
            observed_epoch: { clerk_boot_id: 'boot-a', epoch_seq: 1 },
          },
        }, {
          event_id: 'event/opaque-2',
          broker: 'ibkr',
          event_kind: 'status',
          journal_seq: 6,
          recorded_at_ms: 1_780_000_000_002,
          callback_identity: 'callback/opaque-2',
          lifecycle_state: 'submitted',
          native_order_id: null,
          native_execution_id: null,
          commission_status: 'unknown',
          fee: null,
          receipt: {
            observed_epoch: { clerk_boot_id: 'boot-b', epoch_seq: 2 },
          },
        }],
      )),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
    const fixture = TestBed.createComponent(ClerkTransactionEvidenceDrawerComponent);
    fixture.componentRef.setInput('accountId', 'DU1234567');
    fixture.componentRef.setInput('transaction', summary('DU1234567', 'ctxn_1'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('No Live Binding');
    expect(text).toContain('Broker Connection');
    expect(text).toContain('event/opaque');
    expect(text).not.toContain('NO_LIVE_BINDING');
    expect(text).toContain('boot-a / 1, boot-b / 2');
  });
});
