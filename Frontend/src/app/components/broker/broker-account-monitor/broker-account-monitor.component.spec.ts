import { signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { of } from 'rxjs';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import type {
  AccountReconciliationReceipt,
  AccountTriageResponse,
} from '../../../api/account-reconciliation.types';
import type { AccountTruthResponse, IbkrPositionsSnapshot } from '../../../api/broker-models';
import type { GateResult } from '../../../api/live-instances.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { LiveRunsService } from '../../../services/live-runs.service';
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
  accountTriage = vi.fn().mockResolvedValue(cleanTriage(accountReconciliationReceipt()));
  clearAccountFreeze = vi.fn().mockResolvedValue({
    schema_version: 1,
    account_id: 'DU1234567',
    cleared: true,
    cleared_source: 'account_recovery_proof',
    recovery_id: 'acct-recovery-DU1234567-1',
    receipt_id: 'acct-recon-DU1234567-1',
    gate_result: gateResult(),
    triage: cleanTriage(accountReconciliationReceipt()),
  });
  acceptExposureOverride = vi.fn().mockResolvedValue({
    schema_version: 1,
    account_id: 'DU1234567',
    cleared: true,
    cleared_source: 'account_audited_override',
    override_id: 'acct-exposure-override-DU1234567-1',
    triage: cleanTriage(accountReconciliationReceipt()),
  });
  reconcileAccount = vi.fn().mockResolvedValue(accountReconciliationReceipt());
  positions = vi.fn().mockResolvedValue(positionsSnapshot());
}

class FakeLiveRunsService {
  emergencyFlattenAccount = vi.fn().mockResolvedValue({
    accepted: true,
    process: { state: 'idle' },
  });
}

function routeFragment(fragment: string | null = null) {
  return { provide: ActivatedRoute, useValue: { fragment: of(fragment) } };
}

describe('BrokerAccountMonitorComponent', () => {
  it('runs account reconciliation from the account truth account id', async () => {
    const broker = new FakeBrokerService();
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
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
    expect(screen.getAllByText('Pass').length).toBeGreaterThan(0);
    expect(screen.queryByText('CLEAN')).toBeNull();
  });

  it('marks a latest receipt stale when the monitor clock passes its expiry', async () => {
    const broker = new FakeBrokerService();
    const expiresAtMs = Date.now() + 60_000;
    broker.accountTriage.mockResolvedValue(
      cleanTriage(accountReconciliationReceipt({ expiresAtMs })),
    );
    const view = await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect((await screen.findAllByText('Clean')).length).toBeGreaterThan(0);

    view.fixture.componentInstance.accountReconciliationNowMs.set(expiresAtMs + 1);
    view.fixture.detectChanges();

    expect(screen.getByText('Not Proven')).toBeTruthy();
    expect(
      screen.getByText(
        'Not yet proven: the account reconciliation receipt is stale. Run account reconcile again.',
      ),
    ).toBeTruthy();
    expect(screen.getByText('Not yet proven')).toBeTruthy();
    expect(screen.queryByText('Unknown')).toBeNull();
  });

  it('renders sick-bay conditions with their cure action and refreshes after clearing freeze', async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(frozenTriage());
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Account freeze active')).toBeTruthy();
    expect(screen.getAllByText('Manual Freeze').length).toBeGreaterThan(0);
    expect(screen.getByText('Owner Account DU1234567')).toBeTruthy();
    expect(screen.getByText(/Account sick bay is gating new starts/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /clear freeze/i }));

    await waitFor(() => {
      expect(broker.clearAccountFreeze).toHaveBeenCalledWith('DU1234567');
    });
    await waitFor(() => {
      expect(screen.queryByText('Account freeze active')).toBeNull();
    });
    expect(screen.getByText('No open account conditions.')).toBeTruthy();
  });

  it('opens resolve exposure and records an audited override', async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(exposureFrozenTriage());
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /resolve exposure/i }));

    expect(screen.getByRole('dialog', { name: /resolve exposure/i })).toBeTruthy();
    expect(screen.getAllByText('Owner Bot retired-freezer').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: /accept exposure/i }));

    await waitFor(() => {
      expect(broker.acceptExposureOverride).toHaveBeenCalledWith(
        'DU1234567',
        expect.objectContaining({
          strategy_instance_id: 'retired-freezer',
          run_id: 'run-freeze',
        }),
      );
    });
  });

  it('opens resolve exposure and flattens through the owner bot', async () => {
    const broker = new FakeBrokerService();
    const liveRuns = new FakeLiveRunsService();
    broker.accountTriage.mockResolvedValue(exposureFrozenTriage());
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useValue: liveRuns },
        routeFragment(),
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /resolve exposure/i }));
    fireEvent.click(screen.getByRole('button', { name: /flatten then reconcile/i }));

    await waitFor(() => {
      expect(liveRuns.emergencyFlattenAccount).toHaveBeenCalledWith(
        'retired-freezer',
        { account: 'DU1234567', confirm: true },
      );
    });
    await waitFor(() => {
      expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    });
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
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
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

  it('deep-links the account reconciliation action by URL fragment', async () => {
    const broker = new FakeBrokerService();
    broker.accountTruth.mockImplementation(
      () =>
        new Promise((resolve) => {
          window.setTimeout(() => resolve(accountTruthResponse()), 25);
        }),
    );
    const focus = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(() => undefined);
    const scrollIntoView = vi
      .spyOn(HTMLElement.prototype, 'scrollIntoView')
      .mockImplementation(() => undefined);
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment('account-reconciliation-action'),
      ],
    });

    const button = await screen.findByRole('button', {
      name: /run account reconcile/i,
    });

    await waitFor(() => {
      expect(focus).toHaveBeenCalled();
    });
    expect(button.id).toBe('account-reconciliation-action');
    expect(scrollIntoView).toHaveBeenCalled();
    focus.mockRestore();
    scrollIntoView.mockRestore();
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
    exposure_resolution: 'flat',
    account_truth: truth,
    evidence_refs: [],
    generated_at_ms: generatedAtMs,
    account_truth_generated_at_ms: truth.generated_at_ms,
    expires_at_ms: overrides.expiresAtMs ?? generatedAtMs + 60_000,
    ttl_ms: 60_000,
  };
}

function cleanTriage(receipt: AccountReconciliationReceipt | null = null): AccountTriageResponse {
  const generatedAtMs = Date.now();
  return {
    schema_version: 1,
    generated_at_ms: generatedAtMs,
    account_id: 'DU1234567',
    strategy_instance_id: null,
    summary_headline: 'Account recovery gates passing',
    summary_detail: 'Account DU1234567 has no blocking account triage rows.',
    overall_gate_result: {
      gate_id: 'account.triage',
      status: 'pass',
      source: 'account_triage',
      operator_reason: 'Account DU1234567 has no blocking account triage rows.',
      operator_next_step: 'ACCOUNT_TRIAGE_PASSING',
      evidence_at_ms: generatedAtMs,
    },
    account_reconciliation_receipt: receipt,
    gate_rows: [],
    conditions: [],
    clear_freeze_actionable: false,
    affected_bots: [],
  };
}

function frozenTriage(): AccountTriageResponse {
  const receipt = accountReconciliationReceipt();
  return {
    ...cleanTriage(receipt),
    summary_headline: 'Account recovery needs attention',
    summary_detail: 'manual_freeze',
    overall_gate_result: {
      gate_id: 'account.triage',
      status: 'freeze',
      source: 'manual_freeze',
      operator_reason: 'manual_freeze',
      operator_next_step: 'CLEAR_FREEZE',
      evidence_at_ms: 1_780_000_002_500,
    },
    conditions: [
      {
        condition_type: 'account_freeze',
        scope: 'account',
        owner: {
          owner_type: 'account',
          owner_id: 'DU1234567',
          label: 'Account DU1234567',
          strategy_instance_id: null,
          run_id: null,
          lifecycle_state: null,
        },
        severity: 'critical',
        title: 'Account freeze active',
        detail: 'manual_freeze',
        operator_next_step: 'CLEAR_FREEZE',
        source: 'manual_freeze',
        evidence_at_ms: 1_780_000_002_500,
        evidence_refs: [],
        affected_strategy_instance_ids: ['DEPVALSPYJUL8'],
        cure_action: 'clear_freeze',
      },
    ],
    clear_freeze_actionable: true,
    affected_bots: [
      {
        strategy_instance_id: 'DEPVALSPYJUL8',
        run_id: 'run-1',
        bot_order_namespace: 'bot.DEPVALSPYJUL8',
        lifecycle_state: 'ACTIVE',
      },
    ],
  };
}

function exposureFrozenTriage(): AccountTriageResponse {
  const receipt = accountReconciliationReceipt();
  return {
    ...cleanTriage(receipt),
    summary_headline: 'Account recovery needs attention',
    summary_detail: 'watchdog.flatten_timed_out',
    overall_gate_result: {
      gate_id: 'account.triage',
      status: 'freeze',
      source: 'watchdog_halt_executor',
      operator_reason: 'watchdog.flatten_timed_out',
      operator_next_step: 'CHECK_IBKR',
      evidence_at_ms: 1_780_000_002_500,
    },
    conditions: [
      {
        condition_type: 'exposure_freeze',
        scope: 'account',
        owner: {
          owner_type: 'bot',
          owner_id: 'retired-freezer',
          label: 'Bot retired-freezer',
          strategy_instance_id: 'retired-freezer',
          run_id: 'run-freeze',
          lifecycle_state: 'RETIRED',
        },
        severity: 'critical',
        title: 'Account freeze active',
        detail: 'watchdog.flatten_timed_out',
        operator_next_step: 'CHECK_IBKR',
        source: 'watchdog_halt_executor',
        evidence_at_ms: 1_780_000_002_500,
        evidence_refs: [],
        affected_strategy_instance_ids: [],
        cure_action: 'resolve_exposure',
      },
    ],
    clear_freeze_actionable: false,
    affected_bots: [],
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
