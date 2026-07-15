import { signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { of } from 'rxjs';
import { beforeAll, describe, expect, it, vi } from 'vitest';

import type {
  AccountConditionRow,
  AccountReconciliationReceipt,
  AccountTriageResponse,
} from '../../../api/account-reconciliation.types';
import type { AccountTruthResponse, IbkrPositionsSnapshot } from '../../../api/broker-models';
import type { GateResult } from '../../../api/live-instances.types';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { LiveRunsService } from '../../../services/live-runs.service';
import { formatTimestampDisplay } from '../../../shared/timestamp';
import {
  makeAccountFreezeCondition,
  makeCleanAccountTriage,
  makeFrozenAccountTriage,
} from '../testing/account-triage-fixtures';
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
  updateAccountReconciliationAutomation = vi.fn().mockResolvedValue({
    schema_version: 1,
    account_id: 'DU1234567',
    enabled: true,
    updated_at_ms: 1_780_000_002_500,
    updated_by: 'account-monitor.operator',
  });
  reconcileAccount = vi.fn().mockResolvedValue(accountReconciliationReceipt());
  positions = vi.fn().mockResolvedValue(positionsSnapshot());
  legacyStaleClaimCandidates = vi.fn().mockResolvedValue({
    schema_version: 1,
    account_id: 'DU1234567',
    generated_at_ms: 1_780_000_002_000,
    candidates: [],
  });
  retireLegacyStaleClaim = vi.fn().mockResolvedValue({
    schema_version: 1,
    receipt_id: 'legacy-retirement-claim-1',
    account_id: 'DU1234567',
    strategy_instance_id: 'legacy-spy',
    run_id: 'run-legacy',
    bot_order_namespace: 'learn-ai/legacy-spy/v1',
    symbol: 'SPY',
    claimed_quantity: 1,
    requested_by: 'account-monitor.operator',
    retired_at_ms: 1_780_000_002_000,
  });
}

class FakeLiveRunsService {
  emergencyFlattenAccount = vi.fn().mockResolvedValue({
    accepted: true,
    process: { state: 'idle' },
  });
  getHostRunnerHealth = vi.fn().mockResolvedValue({
    ok: true,
    repo_root: '/repo',
    live_runs_root: '/repo/artifacts/live_runs',
    fetched_at_ms: 1_780_000_000_000,
    process: { state: 'idle', command: [] },
    clerks: [],
  });
  getAccountFleet = vi.fn().mockResolvedValue({
    net_positions: { SPY: 2 },
    explained_total: { SPY: 2 },
    explained_by_instance: [],
    residual: {},
    verdict: 'clean',
    policy_blocks_starts: false,
    summary: 'Broker exposure matches the managed journal.',
  });
}

function routeFragment(fragment: string | null = null) {
  return { provide: ActivatedRoute, useValue: { fragment: of(fragment) } };
}

describe('BrokerAccountMonitorComponent', () => {
  it('shows the managed-versus-broker journal by symbol', async () => {
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: new FakeBrokerService() },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Managed versus broker exposure')).toBeTruthy();
    expect(screen.getByText(/Broker 2.*managed 2.*residual 0/)).toBeTruthy();
  });

  it('shows the matching account clerk lease health', async () => {
    const liveRuns = new FakeLiveRunsService();
    liveRuns.getHostRunnerHealth.mockResolvedValue({
      ok: true,
      repo_root: '/repo',
      live_runs_root: '/repo/artifacts/live_runs',
      fetched_at_ms: 1_780_000_000_000,
      process: { state: 'idle', command: [] },
      clerks: [
        {
          account_id: 'DU1234567',
          generation: 4,
          pid: 42,
          status: 'RUNNING',
          started_at_ms: 1_780_000_000_000,
          renewed_at_ms: 1_780_000_001_000,
          valid_until_ms: 1_780_000_006_000,
          lease_valid: true,
        },
      ],
    });
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: new FakeBrokerService() },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useValue: liveRuns },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Account Clerk')).toBeTruthy();
    expect(screen.getByText(/Generation 4.*lease valid/)).toBeTruthy();
  });

  it('offers only the backend-proven stale claim cure and retires the selected claim', async () => {
    const broker = new FakeBrokerService();
    broker.legacyStaleClaimCandidates
      .mockResolvedValueOnce({
        schema_version: 1,
        account_id: 'DU1234567',
        generated_at_ms: 1_780_000_002_000,
        candidates: [
          {
            claim_id: 'legacy-claim-1',
            strategy_instance_id: 'legacy-spy',
            run_id: 'run-legacy',
            bot_order_namespace: 'learn-ai/legacy-spy/v1',
            symbol: 'SPY',
            claimed_quantity: 1,
            proof_summary: 'LEGACY_CLAIM_BROKER_FLAT:SPY',
            proved_at_ms: 1_780_000_002_000,
          },
        ],
      })
      .mockResolvedValueOnce({
        schema_version: 1,
        account_id: 'DU1234567',
        generated_at_ms: 1_780_000_002_100,
        candidates: [],
      });
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    const retire = await screen.findByRole('button', { name: /retire stale claim/i });
    fireEvent.click(retire);

    await waitFor(() => {
      expect(broker.retireLegacyStaleClaim).toHaveBeenCalledWith('DU1234567', {
        strategy_instance_id: 'legacy-spy',
        run_id: 'run-legacy',
        symbol: 'SPY',
      });
    });
  });

  it('explains when a fleet block has no legacy claim the backend can safely retire yet', async () => {
    const liveRuns = new FakeLiveRunsService();
    liveRuns.getAccountFleet.mockResolvedValue({
      net_positions: {},
      explained_total: { SPY: 1 },
      explained_by_instance: [],
      residual: { SPY: -1 },
      verdict: 'contaminated',
      policy_blocks_starts: true,
      summary: 'Managed bot artifacts overstate broker position(s): SPY -1.',
    });

    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: new FakeBrokerService() },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useValue: liveRuns },
        routeFragment(),
      ],
    });

    expect(
      await screen.findByText(/no stale legacy claim is currently eligible for retirement/i),
    ).toBeTruthy();
  });

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

  it('renders account-observation transitions rather than sweep heartbeats', async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(
      makeCleanAccountTriage({
        receipt: accountReconciliationReceipt(),
        accountObservation: {
          state: 'VERIFIED',
          reason_line: 'Account verified.',
          observed_at_ms: 1_780_000_002_000,
          valid_until_ms: 1_780_000_062_000,
          history: [
            {
              state: 'REVOKED',
              reason_line: 'broker sweep timed out',
              recorded_at_ms: 1_780_000_001_000,
            },
            {
              state: 'VERIFIED',
              reason_line: 'Account verified.',
              recorded_at_ms: 1_780_000_002_000,
            },
          ],
        },
      }),
    );
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Account verification')).toBeTruthy();
    expect(screen.getByText('Account verified.')).toBeTruthy();
    expect(screen.getAllByTestId('account-observation-transition')).toHaveLength(2);
  });

  it('projects the five operator outcomes from account evidence', async () => {
    const broker = new FakeBrokerService();
    const truth = accountTruthResponse({
      responseOverrides: {
        owner_summaries: [
          ownerSummary('Bot active-runner', 'bot', 'ACTIVE', {
            openOrderCount: 1,
            executionCount: 2,
          }),
          ownerSummary('Bot retired-freezer', 'bot', 'RETIRED', {
            positionCount: 1,
            grossPositionQuantity: 1,
          }),
          ownerSummary('Manual ticket T-42', 'manual', 'UNKNOWN', {
            executionCount: 1,
          }),
          ownerSummary('Foreign or unclaimed', 'foreign_or_unclaimed', 'UNKNOWN', {
            positionCount: 1,
            grossPositionQuantity: 1,
          }),
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
        source_freshness: [
          {
            source: 'executions',
            label: 'Executions',
            status: 'stale',
            severity: 'warning',
            fetched_at_ms: 1_780_000_000_000,
            age_ms: 130_000,
            hard_ttl_ms: 120_000,
            reason_code: 'EXECUTIONS_STALE',
            message: 'Execution evidence is stale.',
          },
        ],
      },
    });
    const receipt = accountReconciliationReceipt({
      accountTruth: truth,
      exposureResolution: 'accepted_override',
    });
    broker.accountTruth.mockResolvedValue(truth);
    broker.accountTriage.mockResolvedValue(
      makeFrozenAccountTriage({
        receipt,
        conditions: [
          makeAccountFreezeCondition({
            conditionType: 'exposure_freeze',
            owner: {
              owner_type: 'bot',
              owner_id: 'retired-freezer',
              label: 'Bot retired-freezer',
              strategy_instance_id: 'retired-freezer',
              run_id: 'run-freeze',
              lifecycle_state: 'RETIRED',
            },
            detail: 'watchdog.flatten_timed_out',
            source: 'watchdog_halt_executor',
            cureAction: 'resolve_exposure',
          }),
        ],
        accountObservation: {
          state: 'VERIFIED',
          reason_line: 'Account verified.',
          observed_at_ms: 1_780_000_002_000,
          valid_until_ms: 1_780_000_062_000,
          history: [],
        },
        clearFreezeActionable: false,
      }),
    );

    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Operator outcome projection')).toBeTruthy();
    expect(screen.getByText('Active bot-owned')).toBeTruthy();
    expect(
      screen.getByText('Bot active-runner: ACTIVE, positions 0, open orders 1, executions 2'),
    ).toBeTruthy();
    expect(screen.getByText('Retired bot recovery')).toBeTruthy();
    expect(screen.getAllByText('Watchdog Flatten Timed Out').length).toBeGreaterThan(0);
    const unobservableOutcome = screen
      .getByRole('heading', { name: 'Unobservable account' })
      .closest('section');
    expect(unobservableOutcome?.querySelector('.outcome-evidence .mono')?.textContent).toBe(
      'Executions Stale',
    );
    expect(screen.getByText('Accepted manual override')).toBeTruthy();
    expect(screen.getByText(/Current receipt records exposure/i)).toBeTruthy();
    expect(screen.getByText('Unattributed exposure')).toBeTruthy();
    expect(screen.getByText('SPY +1 (Foreign or unclaimed)')).toBeTruthy();
    expect(screen.getByText('Unobservable account')).toBeTruthy();
  });

  it('marks an expired accepted override as needing attention', async () => {
    const broker = new FakeBrokerService();
    const receipt = accountReconciliationReceipt({
      exposureResolution: 'accepted_override',
      expiresAtMs: Date.now() - 1,
    });
    broker.accountTriage.mockResolvedValue(cleanTriage(receipt));

    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    const heading = await screen.findByRole('heading', { name: 'Accepted manual override' });
    const outcome = heading.closest('section');
    expect(outcome?.textContent).toContain('Attention');
    expect(outcome?.textContent).toContain('The last accepted exposure override is expired.');
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

    expect(screen.getByText('Next account action')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Run account reconcile' })).toBeTruthy();
    expect(
      screen.getByText(
        'Broker account is flat. Run account reconcile to refresh proof, then clear the account freeze.',
      ),
    ).toBeTruthy();
    const reconcileButton = screen.getByRole('button', {
      name: /run account reconcile/i,
    }) as HTMLButtonElement;
    expect(reconcileButton.id).toBe('account-reconciliation-action');
    expect(screen.getByText('Not Proven')).toBeTruthy();
    expect(
      screen.getByText(
        'Not yet proven: the account reconciliation receipt is stale. Run account reconcile again.',
      ),
    ).toBeTruthy();
    expect(screen.getByText('Not yet proven')).toBeTruthy();
    expect(screen.queryByText('Unknown')).toBeNull();
  });

  it('shows when the account was reconciled and how much freshness remains', async () => {
    const generatedAtMs = 1_780_000_002_000;
    const expiresAtMs = generatedAtMs + 300_000;
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(
      cleanTriage(
        accountReconciliationReceipt({
          generatedAtMs,
          expiresAtMs,
          ttlMs: 300_000,
        }),
      ),
    );
    const view = await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    await screen.findByText('Last reconciled');
    view.fixture.componentInstance.accountReconciliationNowMs.set(generatedAtMs + 30_000);
    view.fixture.detectChanges();

    expect(screen.getByText(formatTimestampDisplay(generatedAtMs, { mode: 'local' }))).toBeTruthy();
    expect(screen.getByText('Time remaining')).toBeTruthy();
    expect(screen.getByText('4m 30s')).toBeTruthy();

    view.fixture.componentInstance.accountReconciliationNowMs.set(expiresAtMs + 1);
    view.fixture.detectChanges();

    expect(screen.getByText('Expired')).toBeTruthy();
  });

  it('uses the trade-adjusted reconciliation validity from account triage', async () => {
    const receipt = accountReconciliationReceipt({
      generatedAtMs: 1_780_000_002_000,
      expiresAtMs: 1_780_000_302_000,
    });
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(
      makeCleanAccountTriage({
        receipt,
        reconciliationValidUntilMs: 1_780_000_003_000,
      }),
    );
    const view = await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    await screen.findByText('Fresh until');
    view.fixture.componentInstance.accountReconciliationNowMs.set(1_780_000_003_001);
    view.fixture.detectChanges();

    expect(screen.getByText('Expired')).toBeTruthy();
    expect(screen.getByText('Not Proven')).toBeTruthy();
    expect(
      screen.getByText(formatTimestampDisplay(1_780_000_003_000, { mode: 'local' })),
    ).toBeTruthy();
  });

  it('enables automatic reconciliation for bot-owned trades from the UI', async () => {
    const broker = new FakeBrokerService();
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    const checkbox = (await screen.findByRole('checkbox', {
      name: 'Auto-reconcile after bot trades',
    })) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    fireEvent.click(checkbox);

    await waitFor(() => {
      expect(broker.updateAccountReconciliationAutomation).toHaveBeenCalledWith('DU1234567', {
        enabled: true,
      });
      expect(checkbox.checked).toBe(true);
    });
  });

  it('restores the saved auto-reconcile setting when the update fails', async () => {
    const broker = new FakeBrokerService();
    broker.updateAccountReconciliationAutomation.mockRejectedValueOnce(
      new Error('policy write failed'),
    );
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });
    const checkbox = (await screen.findByRole('checkbox', {
      name: 'Auto-reconcile after bot trades',
    })) as HTMLInputElement;

    fireEvent.click(checkbox);

    expect((await screen.findByRole('alert')).textContent).toContain(
      "We couldn't save the auto-reconcile setting.",
    );
    expect(checkbox.checked).toBe(false);
  });

  it('promotes exposure resolution after reconciliation returns unresolved exposure', async () => {
    const broker = new FakeBrokerService();
    const liveRuns = new FakeLiveRunsService();
    const receipt = accountReconciliationReceipt({
      accountTruth: unresolvedSpyAccountTruth(),
      exposureResolution: 'unresolved',
      gate: {
        status: 'block',
        operator_reason:
          'Account Truth is otherwise clean, but broker exposure is not flat. exposure_resolution=unresolved.',
        operator_next_step: 'RESOLVE_EXPOSURE',
      },
      state: 'NOT_PROVEN',
    });
    broker.accountTruth.mockResolvedValue(unresolvedSpyAccountTruth());
    broker.accountTriage.mockResolvedValue(exposureFrozenTriage(receipt));
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useValue: liveRuns },
        routeFragment(),
      ],
    });

    expect(
      await screen.findByRole('heading', {
        name: 'Flatten unresolved exposure',
      }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        'SPY +1 (Foreign or unclaimed) remains unresolved. Use Resolve exposure on this page to flatten through the owner bot, wait for the fill, then run account reconcile again.',
      ),
    ).toBeTruthy();

    const primaryButton = document.getElementById('account-primary-action') as HTMLButtonElement;
    expect(primaryButton.textContent).toContain('Resolve exposure');
    fireEvent.click(primaryButton);
    expect(screen.getByRole('dialog', { name: /resolve exposure/i })).toBeTruthy();

    broker.accountTriage.mockClear();
    fireEvent.click(screen.getByRole('button', { name: /flatten then reconcile/i }));
    await waitFor(() => {
      expect(liveRuns.emergencyFlattenAccount).toHaveBeenCalledWith('retired-freezer', {
        account: 'DU1234567',
        confirm: true,
      });
    });
    expect(screen.getByRole('button', { name: /run account reconcile/i }).id).toBe(
      'account-reconciliation-action',
    );
  });

  it('promotes reconciliation after the flatten order leaves the account flat', async () => {
    const broker = new FakeBrokerService();
    const unresolvedReceipt = accountReconciliationReceipt({
      accountTruth: unresolvedSpyAccountTruth(),
      exposureResolution: 'unresolved',
      gate: {
        status: 'block',
        operator_reason:
          'Account Truth is otherwise clean, but broker exposure is not flat. exposure_resolution=unresolved.',
        operator_next_step: 'RESOLVE_EXPOSURE',
      },
      state: 'NOT_PROVEN',
    });
    broker.accountTruth.mockResolvedValue(accountTruthResponse());
    broker.accountTriage.mockResolvedValue(cleanTriage(unresolvedReceipt));
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByRole('heading', { name: 'Run account reconcile' })).toBeTruthy();
    expect(
      screen.getByText(
        'Broker account is flat now. Run account reconcile to record the filled flatten order, then clear the account freeze.',
      ),
    ).toBeTruthy();
    expect(screen.queryByRole('link', { name: /open flatten ticket/i })).toBeNull();
  });

  it('keeps account recovery prominent and collapses bot-specific history', async () => {
    const broker = new FakeBrokerService();
    const expiredReceipt = accountReconciliationReceipt({
      expiresAtMs: Date.now() - 1,
    });
    broker.accountTriage.mockResolvedValue(mixedFrozenTriage(expiredReceipt));
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Account freeze active')).toBeTruthy();
    expect(
      screen.getByText(
        'Broker account is flat. Account sick bay is waiting for a fresh reconciliation receipt.',
      ),
    ).toBeTruthy();
    expect(screen.getByText('Bot-specific history')).toBeTruthy();
    expect(screen.getByText('1')).toBeTruthy();
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
    expect(screen.getByRole('heading', { name: 'Clear account freeze' })).toBeTruthy();
    expect(screen.getAllByText('Manual Freeze').length).toBeGreaterThan(0);
    expect(screen.getByText('Owner Account DU1234567')).toBeTruthy();
    expect(screen.getByText(/Account sick bay is gating new starts/)).toBeTruthy();

    const clearFreezeButton = screen.getByRole('button', {
      name: /clear freeze/i,
    });
    expect(clearFreezeButton.id).toBe('account-clear-freeze-action');
    fireEvent.click(clearFreezeButton);

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
    expect(screen.getByText('Strategy instance')).toBeTruthy();
    expect(screen.getByText('retired-freezer')).toBeTruthy();
    expect(screen.getByText('Run')).toBeTruthy();
    expect(screen.getByText('run-freeze')).toBeTruthy();
    expect(screen.getByText('Lifecycle')).toBeTruthy();
    expect(screen.getByText('RETIRED')).toBeTruthy();
    const reviveButton = screen.getByRole('button', {
      name: /revive same bot/i,
    }) as HTMLButtonElement;
    expect(reviveButton.disabled).toBe(true);

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
    broker.accountTriage.mockClear();
    fireEvent.click(screen.getByRole('button', { name: /flatten then reconcile/i }));

    await waitFor(() => {
      expect(liveRuns.emergencyFlattenAccount).toHaveBeenCalledWith('retired-freezer', {
        account: 'DU1234567',
        confirm: true,
      });
    });
    await waitFor(() => {
      expect(broker.reconcileAccount).toHaveBeenCalledWith('DU1234567');
    });
    expect(broker.accountTriage).toHaveBeenCalledTimes(1);
  });

  it('disables audited exposure override when the exposure owner is ambiguous', async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(ambiguousExposureFrozenTriage());
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /resolve exposure/i }));

    const acceptButton = screen.getByRole('button', {
      name: /accept exposure/i,
    }) as HTMLButtonElement;
    expect(acceptButton.disabled).toBe(true);
    fireEvent.click(acceptButton);
    expect(broker.acceptExposureOverride).not.toHaveBeenCalled();
  });

  it('renders unsupported condition cures without dead action buttons', async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage.mockResolvedValue(terminalTriage());
    await render(BrokerAccountMonitorComponent, {
      providers: [
        { provide: BrokerService, useValue: broker },
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: LiveRunsService, useClass: FakeLiveRunsService },
        routeFragment(),
      ],
    });

    expect(await screen.findByText('Bot crashed')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /retire/i })).toBeNull();
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
  overrides: {
    accountTruth?: AccountTruthResponse;
    exposureResolution?: AccountReconciliationReceipt['exposure_resolution'];
    expiresAtMs?: number;
    generatedAtMs?: number;
    gate?: Partial<GateResult>;
    state?: AccountReconciliationReceipt['state'];
    ttlMs?: number;
  } = {},
): AccountReconciliationReceipt {
  const truth = overrides.accountTruth ?? accountTruthResponse();
  const generatedAtMs = overrides.generatedAtMs ?? Date.now();
  const ttlMs = overrides.ttlMs ?? 300_000;
  return {
    schema_version: 1,
    receipt_id: 'acct-recon-DU1234567-1',
    account_id: 'DU1234567',
    requested_account_id: 'DU1234567',
    connected_account_id: 'DU1234567',
    state: overrides.state ?? 'CLEAN',
    account_truth_verdict: truth.final_verdict,
    account_truth_severity: truth.final_severity,
    final_gate_result: gateResult(overrides.gate),
    exposure_resolution: overrides.exposureResolution ?? 'flat',
    account_truth: truth,
    evidence_refs: [],
    generated_at_ms: generatedAtMs,
    account_truth_generated_at_ms: truth.generated_at_ms,
    expires_at_ms: overrides.expiresAtMs ?? generatedAtMs + ttlMs,
    ttl_ms: ttlMs,
  };
}

function cleanTriage(receipt: AccountReconciliationReceipt | null = null): AccountTriageResponse {
  return makeCleanAccountTriage({ receipt });
}

function frozenTriage(): AccountTriageResponse {
  const receipt = accountReconciliationReceipt();
  return makeFrozenAccountTriage({
    receipt,
    freezeBanner: {
      headline: 'Account sick bay is gating new starts.',
      detail: 'Run account reconciliation and clear the active account freeze before deploying.',
    },
    affectedBots: [
      {
        strategy_instance_id: 'DEPVALSPYJUL8',
        run_id: 'run-1',
        bot_order_namespace: 'bot.DEPVALSPYJUL8',
        lifecycle_state: 'ACTIVE',
      },
    ],
  });
}

function exposureFrozenTriage(
  receipt: AccountReconciliationReceipt = accountReconciliationReceipt(),
): AccountTriageResponse {
  return makeFrozenAccountTriage({
    receipt,
    conditionOptions: {
      conditionType: 'exposure_freeze',
      owner: {
        owner_type: 'bot',
        owner_id: 'retired-freezer',
        label: 'Bot retired-freezer',
        strategy_instance_id: 'retired-freezer',
        run_id: 'run-freeze',
        lifecycle_state: 'RETIRED',
      },
      detail: 'watchdog.flatten_timed_out',
      operatorNextStep: 'CHECK_IBKR',
      source: 'watchdog_halt_executor',
      affectedStrategyInstanceIds: [],
      cureAction: 'resolve_exposure',
    },
    freezeBanner: {
      headline: 'Account sick bay is gating new starts.',
      detail: 'Resolve or audit broker exposure before starting another bot on this account.',
    },
    clearFreezeActionable: false,
  });
}

function ambiguousExposureFrozenTriage(): AccountTriageResponse {
  return makeFrozenAccountTriage({
    receipt: accountReconciliationReceipt(),
    condition: makeAccountFreezeCondition({
      conditionType: 'exposure_freeze',
      detail: 'watchdog.flatten_timed_out',
      operatorNextStep: 'CHECK_IBKR',
      source: 'watchdog_halt_executor',
      affectedStrategyInstanceIds: [],
      cureAction: 'resolve_exposure',
    }),
    freezeBanner: {
      headline: 'Account sick bay is gating new starts.',
      detail: 'Resolve or audit broker exposure before starting another bot on this account.',
    },
    clearFreezeActionable: false,
  });
}

function mixedFrozenTriage(receipt: AccountReconciliationReceipt): AccountTriageResponse {
  return makeFrozenAccountTriage({
    receipt,
    conditions: [
      makeAccountFreezeCondition({
        detail: 'watchdog.flatten_timed_out',
        source: 'watchdog_halt_executor',
        affectedStrategyInstanceIds: ['DEPVALSPYJUL8'],
        cureAction: 'clear_freeze',
      }),
      botCrashCondition(),
    ],
    clearFreezeActionable: false,
  });
}

function botCrashCondition(): AccountConditionRow {
  return {
    condition_type: 'crashed',
    scope: 'bot',
    owner: {
      owner_type: 'bot',
      owner_id: 'retired-freezer',
      label: 'Bot retired-freezer',
      strategy_instance_id: 'retired-freezer',
      run_id: 'run-freeze',
      lifecycle_state: 'RETIRED',
    },
    severity: 'critical',
    title: 'Bot crashed',
    detail: 'retired-freezer ended from a crash in run run-freeze.',
    operator_next_step: 'RETIRE_REPLACE',
    source: 'host_daemon.process_crashed',
    evidence_at_ms: 1_780_000_002_500,
    evidence_refs: [],
    affected_strategy_instance_ids: [],
    cure_action: 'retire_replace',
  };
}

function terminalTriage(): AccountTriageResponse {
  return {
    ...makeCleanAccountTriage({ receipt: accountReconciliationReceipt() }),
    summary_headline: 'Account recovery needs attention',
    summary_detail: 'A retired bot needs operator attention.',
    conditions: [botCrashCondition()],
  };
}

function unresolvedSpyAccountTruth(): AccountTruthResponse {
  return {
    ...accountTruthResponse(),
    final_verdict: 'not_proven',
    final_severity: 'critical',
    status_label: 'Not proven',
    status_detail: 'Bot submits should stay blocked until critical account truth blockers clear.',
    blockers: [
      {
        code: 'unknown_positions',
        severity: 'critical',
        title: 'Unknown current broker positions',
        message:
          'At least one current IBKR position is not explained by known bot/manual evidence.',
        forensic_facts: { count: 1 },
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
  };
}

function ownerSummary(
  ownerLabel: string,
  ownerClass: AccountTruthResponse['owner_summaries'][number]['owner_class'],
  ownerBindingState: AccountTruthResponse['owner_summaries'][number]['owner_binding_state'],
  overrides: {
    openOrderCount?: number;
    executionCount?: number;
    positionCount?: number;
    grossPositionQuantity?: number;
  } = {},
): AccountTruthResponse['owner_summaries'][number] {
  return {
    owner_class: ownerClass,
    owner_key: ownerLabel.toLowerCase().replaceAll(' ', '-'),
    owner_label: ownerLabel,
    evidence_tier:
      ownerClass === 'bot'
        ? 'bot_order_ref'
        : ownerClass === 'manual'
          ? 'app_minted_manual'
          : ownerClass,
    evidence_label: ownerLabel,
    owner_binding_state: ownerBindingState,
    open_order_count: overrides.openOrderCount ?? 0,
    execution_count: overrides.executionCount ?? 0,
    position_count: overrides.positionCount ?? 0,
    gross_position_quantity: overrides.grossPositionQuantity ?? 0,
  };
}

function accountTruthResponse(
  overrides: {
    accountId?: string | null;
    healthAccountId?: string | null;
    responseOverrides?: Partial<AccountTruthResponse>;
  } = {},
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
    operator_blockers: [],
    caveats: [],
    owner_summaries: [],
    symbol_exposures: [],
    orders: [],
    executions: [],
    positions: [],
    evidence_gaps: [],
    source_freshness: [],
    ...overrides.responseOverrides,
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
