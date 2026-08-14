import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import type {
  ClerkCustodyTimeline,
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
  overrides: Partial<ClerkTransactionDetail> = {},
): ClerkTransactionDetail {
  const { event_count: _, ...transaction } = summary(accountId, transactionId);
  return { ...transaction, receipt, events, custody_timeline: null, ...overrides };
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

  it('shows the custody lifecycle first and keeps evidence accordions collapsed until opened', async () => {
    const broker = {
      accountTransaction: vi.fn().mockResolvedValue(detail(
        'DU1234567',
        'ctxn_2',
        { receipt_hash: 'receipt/opaque' },
        [],
        {
          lifecycle_state: 'filled',
          order_ref: 'order/opaque',
          order_instruction: {
            symbol: 'SPY',
            sec_type: 'STK',
            action: 'buy',
            quantity: 2,
            order_type: 'market',
            limit_price: null,
            stop_price: null,
            time_in_force: 'day',
            outside_rth: false,
          },
          execution_quantity: 2,
          execution_price: 601.25,
          custody_timeline: custodyTimeline(),
        },
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
    fixture.componentRef.setInput('transaction', summary('DU1234567', 'ctxn_2'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('app-asset-identity')?.textContent).toContain('SPY');
    expect(host.textContent).toContain('Custody lifecycle');
    expect(host.textContent).toContain('Custody accepted');
    expect(host.textContent).toContain('Economic terminal');

    const instructionHeader = accordionHeader(host, 'Instruction and execution');
    const eventsHeader = accordionHeader(host, 'Event log');
    const rawHeader = accordionHeader(host, 'Raw receipt evidence');
    expect(instructionHeader.getAttribute('aria-expanded')).toBe('false');
    expect(eventsHeader.getAttribute('aria-expanded')).toBe('false');
    expect(rawHeader.getAttribute('aria-expanded')).toBe('false');

    instructionHeader.click();
    fixture.detectChanges();
    expect(instructionHeader.getAttribute('aria-expanded')).toBe('true');
    expect(host.querySelector('.receipt-table')?.textContent).toContain('Requested instruction');
  });

  it('renders the durable manual custody subject instead of inventing a bot manager', async () => {
    const broker = {
      accountTransaction: vi.fn().mockResolvedValue(detail(
        'PA1',
        'manual-effect',
        {},
        [],
        { subject_id: 'manual-operator:operator-42', strategy_instance_id: null },
      )),
    };
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
    const fixture = TestBed.createComponent(ClerkTransactionEvidenceDrawerComponent);
    fixture.componentRef.setInput('accountId', 'PA1');
    fixture.componentRef.setInput('transaction', summary('PA1', 'manual-effect'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Custody subject');
    expect(text).toContain('manual-operator:operator-42');
  });
});

function accordionHeader(host: HTMLElement, label: string): HTMLElement {
  const button = Array.from(host.querySelectorAll<HTMLElement>('[role="button"]')).find(
    (candidate) => candidate.textContent?.includes(label),
  );
  if (button === undefined) throw new Error(`Accordion header not found: ${label}`);
  return button;
}

function custodyTimeline(): ClerkCustodyTimeline {
  return {
    intent_created_at_ms: 1_780_000_000_000,
    clerk_request_received_at_ms: 1_780_000_000_001,
    clerk_intake_admitted_at_ms: 1_780_000_000_002,
    inbox_fsynced_at_ms: 1_780_000_000_003,
    a0_custody_accepted_at_ms: 1_780_000_000_004,
    broker_write_started_at_ms: 1_780_000_000_005,
    broker_call_returned_at_ms: 1_780_000_000_006,
    broker_ack_recorded_at_ms: 1_780_000_000_007,
    earliest_broker_source_at_ms: 1_780_000_000_008,
    first_callback_arrived_at_ms: 1_780_000_000_009,
    first_callback_recorded_at_ms: 1_780_000_000_010,
    economic_terminal_recorded_at_ms: 1_780_000_000_011,
    durations: {
      request_to_intake_ms: 1,
      intake_to_a0_ms: 2,
      a0_to_broker_write_ms: 1,
      broker_write_to_return_ms: 1,
      broker_return_to_first_callback_ms: 2,
      terminal_age_ms: 1,
    },
  };
}
