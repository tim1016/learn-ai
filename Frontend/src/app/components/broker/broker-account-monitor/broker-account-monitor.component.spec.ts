import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import type { AccountReconciliationReceipt } from '../../../api/account-reconciliation.types';
import type { AccountTruthResponse, IbkrPositionsSnapshot } from '../../../api/broker-models';
import type { GateResult } from '../../../api/live-instances.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { BrokerAccountMonitorComponent } from './broker-account-monitor.component';

class StubEventSource {
  addEventListener = vi.fn();
  close = vi.fn();
}

beforeAll(() => {
  vi.stubGlobal('EventSource', StubEventSource);
});

class FakeBrokerHealthService {
  readonly bannerState = signal<string | null>(null);
  readonly health = signal({
    connected: true,
    is_paper: true,
    account_id: 'DU1234567',
    mode: 'paper' as const,
    host: '127.0.0.1',
    port: 4002,
    client_id: 1,
    server_version: 178,
  });
}

class FakeBrokerService {
  accountTruth = vi.fn().mockResolvedValue(accountTruthResponse());
  latestAccountReconciliation = vi.fn().mockRejectedValue(new HttpErrorResponse({ status: 404 }));
  reconcileAccount = vi.fn().mockResolvedValue(accountReconciliationReceipt());
  positions = vi.fn().mockResolvedValue(positionsSnapshot());
}

describe('BrokerAccountMonitorComponent', () => {
  it('runs account reconciliation from the account truth account id', async () => {
    const broker = new FakeBrokerService();
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
      ],
    });

    const button = await screen.findByRole('button', {
      name: /run account reconcile/i,
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    });
    expect(await screen.findByText('Account Truth is clean.')).toBeTruthy();
    expect(screen.getAllByText('Clean').length).toBeGreaterThan(0);
    expect(screen.getByText('Pass')).toBeTruthy();
    expect(screen.queryByText('CLEAN')).toBeNull();
  });

  it('marks a latest receipt stale when the monitor clock passes its expiry', async () => {
    const broker = new FakeBrokerService();
    const expiresAtMs = Date.now() + 60_000;
    broker.latestAccountReconciliation.mockResolvedValue(
      accountReconciliationReceipt({ expiresAtMs }),
    );
    const view = await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
      ],
    });

    expect((await screen.findAllByText('Clean')).length).toBeGreaterThan(0);

    view.fixture.componentInstance.accountReconciliationNowMs.set(expiresAtMs + 1);
    view.fixture.detectChanges();

    expect(screen.getByText('Stale')).toBeTruthy();
    expect(
      screen.getByText('Receipt expired before this account monitor snapshot. Run account reconcile again.'),
    ).toBeTruthy();
    expect(screen.getByText('Unknown')).toBeTruthy();
  });

  it('runs fail-closed reconciliation from broker health account when truth account is unknown', async () => {
    const broker = new FakeBrokerService();
    broker.accountTruth.mockResolvedValue(
      accountTruthResponse({ accountId: null, healthAccountId: 'DU1234567' }),
    );
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
      ],
    });

    const button = await screen.findByRole('button', {
      name: /run account reconcile/i,
    });
    fireEvent.click(button);

    await waitFor(() => {
      expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    });
  });
});

function gateResult(overrides: Partial<GateResult> = {}): GateResult {
  return {
    gate_id: 'account.reconciliation',
    status: 'pass',
    source: 'account_reconciliation_receipt',
    operator_reason: 'Account Truth is clean.',
    operator_next_step: 'ACCOUNT_CLEAN',
    evidence_at_ms: 1_780_000_002_000,
    ...overrides,
  };
}

function accountReconciliationReceipt(
  overrides: { expiresAtMs?: number } = {},
): AccountReconciliationReceipt {
  const truth = accountTruthResponse();
  const generatedAtMs = Date.now();
  return {
    schema_version: 1,
    receipt_id: 'acct-recon-DU1234567-1',
    account_id: 'DU1234567',
    requested_account_id: 'DU1234567',
    connected_account_id: 'DU1234567',
    state: 'CLEAN',
    account_truth_verdict: 'clean',
    account_truth_severity: 'ok',
    final_gate_result: gateResult(),
    account_truth: truth,
    evidence_refs: [],
    generated_at_ms: generatedAtMs,
    account_truth_generated_at_ms: truth.generated_at_ms,
    expires_at_ms: overrides.expiresAtMs ?? generatedAtMs + 60_000,
    ttl_ms: 60_000,
  };
}

function accountTruthResponse(
  overrides: { accountId?: string | null; healthAccountId?: string | null } = {},
): AccountTruthResponse {
  const accountId = overrides.accountId === undefined ? 'DU1234567' : overrides.accountId;
  const healthAccountId =
    overrides.healthAccountId === undefined ? 'DU1234567' : overrides.healthAccountId;
  return {
    account_id: accountId,
    final_verdict: 'clean',
    final_severity: 'ok',
    status_label: 'Clean',
    status_detail: 'Required live broker evidence is assigned to known ownership.',
    generated_at_ms: 1_780_000_001_000,
    health: {
      mode: 'paper',
      host: '127.0.0.1',
      port: 4002,
      client_id: 7,
      connected: true,
      disabled: false,
      reason: null,
      account_id: healthAccountId,
      is_paper: true,
      server_version: 178,
      fetched_at_ms: 1_780_000_000_000,
      safety_verdict: {
        configured_mode: 'paper',
        readonly_flag: false,
        port_class: 'paper_port',
        connected_account_prefix: 'DU',
        final_verdict: 'paper-only',
        failing_gates: [],
        unknown_gates: [],
      },
      connection_state: 'connected',
      last_transition_ms: 1_780_000_000_000,
      connection_lost: false,
      connectivity_lost_count: 0,
      reconnect_attempt: null,
    },
    account: null,
    known_bot_namespaces: [],
    manual_namespaces_observed: [],
    invariants: [],
    blockers: [],
    caveats: [],
    owner_summaries: [],
    symbol_exposures: [],
    orders: [],
    executions: [],
    positions: [],
    evidence_gaps: [],
    source_freshness: [],
  };
}

function positionsSnapshot(): IbkrPositionsSnapshot {
  return {
    account_id: 'DU1234567',
    is_paper: true,
    positions: [],
    fetched_at_ms: 1_780_000_001_000,
    used_cache_fallback: false,
  };
}
