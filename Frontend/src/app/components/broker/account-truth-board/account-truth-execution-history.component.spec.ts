import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import type {
  AccountTruthExecutionRow,
  AccountTruthFactOwner,
} from '../../../api/broker-models';
import { AccountTruthExecutionHistoryComponent } from './account-truth-execution-history.component';

const BOT_OWNER: AccountTruthFactOwner = {
  owner_class: 'bot',
  owner_key: 'bot-a',
  owner_label: 'Bot A',
  evidence_tier: 'bot_order_ref',
  evidence_label: 'Bot-stamped order ref',
  owner_binding_state: 'ACTIVE',
  severity: 'ok',
};

function execution(
  overrides: Partial<AccountTruthExecutionRow> = {},
): AccountTruthExecutionRow {
  return {
    fact_kind: 'execution',
    account_id: 'DU1234567',
    exec_id: 'exec-1',
    order_id: 42,
    perm_id: 9001,
    client_id: 7,
    con_id: 12345,
    symbol: 'SPY',
    side: 'BUY',
    order_type: 'MKT',
    quantity: 2,
    price: 450.25,
    fee: 1.25,
    exec_time_ms: Date.UTC(2026, 6, 1, 15, 30, 0),
    observed_at_ms: Date.UTC(2026, 6, 1, 15, 30, 1),
    order_ref: 'learn-ai/bot-a/v1:intent-1',
    owner: BOT_OWNER,
    headline: 'SPY execution',
    detail: 'SPY execution detail',
    ibkr_evidence: null,
    ...overrides,
  };
}

describe('AccountTruthExecutionHistoryComponent', () => {
  it('renders broker execution details grouped by owner and day', () => {
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(AccountTruthExecutionHistoryComponent);
    fixture.componentRef.setInput('executions', [execution()]);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Execution history');
    expect(text).toContain('Broker direct');
    expect(text).toContain('Bot attribution');
    expect(text).toContain('Stamped and echoed');
    expect(text).toContain('Bot A');
    expect(text).toContain('2026-07-01');
    expect(text).toContain('SPY');
    expect(text).toContain('$450.25');
    expect(text).toContain('Broker order ID');
    expect(text).toContain('Broker exec ID');
    expect(text).toContain('learn-ai/bot-a/v1:intent-1');
    expect(text).toContain('42');
    expect(text).toContain('exec-1');
    expect(text).not.toContain('Binding');
    expect(text).not.toContain('Evidence');
  });

  it('points out row-level broker-field uncertainty', () => {
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(AccountTruthExecutionHistoryComponent);
    fixture.componentRef.setInput('executions', [
      execution({
        exec_id: 'exec-foreign',
        order_ref: null,
        exec_time_ms: null,
        fee: null,
        quantity: null,
        price: null,
        owner: {
          owner_class: 'foreign_or_unclaimed',
          owner_key: 'foreign_or_unclaimed',
          owner_label: 'Foreign or unclaimed',
          evidence_tier: 'foreign_or_unclaimed',
          evidence_label: 'No known ownership evidence',
          owner_binding_state: 'UNKNOWN',
          severity: 'critical',
        },
      }),
    ]);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Foreign or unclaimed');
    expect(text).toContain('Missing Order Ref');
    expect(text).toContain('Observed Time Only');
    expect(text).toContain('Commission Pending');
    expect(text).toContain('Missing Quantity');
    expect(text).toContain('Missing Price');
  });
});
