import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';
import type { AccountTruthResponse } from '../../../api/broker-models';
import { AccountTruthBoardComponent } from './account-truth-board.component';

function truth(overrides: Partial<AccountTruthResponse> = {}): AccountTruthResponse {
  return {
    account_id: 'DU1234567',
    final_verdict: 'not_proven',
    final_severity: 'critical',
    status_label: 'Not proven',
    status_detail: 'Bot submits should stay blocked until critical blockers clear.',
    generated_at_ms: 1_780_000_000_000,
    health: {
      mode: 'paper',
      host: '127.0.0.1',
      port: 4002,
      client_id: 7,
      connected: true,
      account_id: 'DU1234567',
      is_paper: true,
      fetched_at_ms: 1_780_000_000_000,
      connection_state: 'connected',
      last_transition_ms: 1_780_000_000_000,
    },
    account: {
      account_id: 'DU1234567',
      is_paper: true,
      base_currency: 'USD',
      cash_balance: 1000,
      net_liquidation: 2000,
      buying_power: 3000,
      init_margin: null,
      maint_margin: 400,
      excess_liquidity: null,
      equity_with_loan_value: null,
      day_pnl: null,
      unrealized_pnl: null,
      realized_pnl: null,
      fetched_at_ms: 1_780_000_000_000,
    },
    known_bot_namespaces: [],
    manual_namespaces_observed: [],
    invariants: [
      {
        key: 'open_orders_known',
        label: 'Open orders known',
        status: 'fail',
        severity: 'critical',
        headline: 'One or more live open orders are foreign or unclaimed.',
        narrative: 'One or more live open orders are foreign or unclaimed.',
        checked_at_ms: 1_780_000_000_000,
        evidence_count: 1,
      },
    ],
    blockers: [
      {
        code: 'unknown_open_orders',
        severity: 'critical',
        title: 'Unknown open broker orders',
        message: 'At least one live IBKR order has no known namespace.',
        forensic_facts: {},
      },
    ],
    caveats: [],
    owner_summaries: [
      {
        owner_class: 'bot',
        owner_key: 'bot-a',
        owner_label: 'Bot A',
        evidence_tier: 'bot_order_ref',
        evidence_label: 'Bot-stamped order ref',
        owner_binding_state: 'ACTIVE',
        open_order_count: 1,
        execution_count: 1,
        position_count: 0,
        gross_position_quantity: 0,
      },
    ],
    symbol_exposures: [
      {
        symbol: 'SPY',
        owner_class: 'foreign_or_unclaimed',
        owner_key: 'foreign_or_unclaimed',
        owner_label: 'Foreign or unclaimed',
        quantity: 1,
        con_id: 756733,
      },
    ],
    orders: [],
    executions: [
      {
        fact_kind: 'execution',
        account_id: 'DU1234567',
        exec_id: 'exec-1',
        order_id: 42,
        perm_id: 9001,
        client_id: 7,
        con_id: 756733,
        symbol: 'SPY',
        side: 'BUY',
        order_type: 'MKT',
        quantity: 1,
        price: 450.25,
        fee: 1.25,
        exec_time_ms: 1_780_000_000_200,
        observed_at_ms: 1_780_000_000_300,
        order_ref: 'learn-ai/bot-a/v1:intent-1',
        owner: {
          owner_class: 'bot',
          owner_key: 'bot-a',
          owner_label: 'Bot A',
          evidence_tier: 'bot_order_ref',
          evidence_label: 'Bot-stamped order ref',
          owner_binding_state: 'ACTIVE',
          severity: 'ok',
        },
        headline: 'SPY execution',
        detail: 'SPY execution detail',
        uncertainty_codes: [],
        ibkr_evidence: null,
      },
    ],
    positions: [],
    evidence_gaps: [],
    ...overrides,
  };
}

describe('AccountTruthBoardComponent', () => {
  it('renders backend-authored verdict, blockers, and invariant narratives', () => {
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(AccountTruthBoardComponent);
    fixture.componentRef.setInput('truth', truth());
    fixture.componentRef.setInput('showInvariants', true);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Not proven');
    expect(text).toContain('Unknown open broker orders');
    expect(text).toContain('One or more live open orders are foreign or unclaimed.');
  });

  it('renders optional account, owner, and exposure sections', () => {
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(AccountTruthBoardComponent);
    fixture.componentRef.setInput('truth', truth());
    fixture.componentRef.setInput('showAccountMetrics', true);
    fixture.componentRef.setInput('showOwnerSummary', true);
    fixture.componentRef.setInput('showSymbolExposures', true);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Net liquidation');
    expect(text).toContain('Bot A');
    expect(text).toContain('Active');
    expect(text).toContain('SPY');
  });

  it('renders execution history when requested', () => {
    TestBed.configureTestingModule({});
    const fixture = TestBed.createComponent(AccountTruthBoardComponent);
    fixture.componentRef.setInput('truth', truth());
    fixture.componentRef.setInput('showExecutionHistory', true);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Execution history');
    expect(text).toContain('learn-ai/bot-a/v1:intent-1');
    expect(text).toContain('exec-1');
  });
});
