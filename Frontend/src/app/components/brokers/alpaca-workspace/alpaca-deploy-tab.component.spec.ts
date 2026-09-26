import { signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { CurrentUrlService } from '../../../shell/current-url.service';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { render, screen, fireEvent } from '@testing-library/angular';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import { AlpacaDeployTabComponent } from './alpaca-deploy-tab.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { BrokersService } from '../../../services/brokers.service';
import { DEPLOY_VIEW } from '../../broker/broker-deploy-page/alpaca-deploy-workflow.fixtures';
import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../fleet/resource-target';

const TARGET = resourceTarget('alpaca', 'clrk_deploy_tab', {
  accountId: 'PA9', bindingGeneration: 3, routingEpoch: 7,
});

function fakeAccount(): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA9',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 10_000,
    equity: 15_000,
    buying_power: 30_000,
    portfolio_value: 15_000,
    long_market_value: 5_000,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: 1_600_000_000_000,
    observed_at_ms: 1_700_000_000_000,
  };
}

function activatedRoute(clerkId: string) {
  const initial = convertToParamMap({ clerkId });
  const noQuery = convertToParamMap({});
  return {
    provide: ActivatedRoute,
    // `AlpacaDeployWorkflowComponent` mounts as a child of this component and
    // reads its own `?strategy=` off the same injected `ActivatedRoute`, so
    // this fake has to answer `queryParamMap` too, not just `paramMap`.
    useValue: {
      paramMap: of(initial),
      snapshot: { paramMap: initial, queryParamMap: noQuery },
      queryParamMap: of(noQuery),
    },
  };
}

interface AccountDataDouble {
  readonly hasAccount: boolean;
}

function accountData({ hasAccount }: AccountDataDouble) {
  return {
    provide: AlpacaDeskAccountDataService,
    useValue: {
      target: () => TARGET,
      accountId: () => 'PA9',
      fence: () => ({ bindingGeneration: 3, routingEpoch: 7 }),
      account: { hasValue: () => hasAccount, value: () => fakeAccount() },
    },
  };
}

async function renderDeployTab(options: {
  readonly lane?: Parameters<typeof testLane>[0] | null;
  readonly hasAccount?: boolean;
} = {}) {
  const { lane = {}, hasAccount = true } = options;
  return render(AlpacaDeployTabComponent, {
    providers: [
      provideRouter([]),
      activatedRoute('clrk_deploy_tab'),
      accountData({ hasAccount }),
      lane === null
        ? provideFleetDirectory({ observed_at_ms: 1_757_000_000_000, clerks: [] })
        : provideFleetDirectory({ observed_at_ms: 1_757_000_000_000, clerks: [testLane({ clerk_id: 'clrk_deploy_tab', ...lane })] }),
      { provide: BrokersService, useValue: { getAccount: vi.fn().mockResolvedValue(fakeAccount()) } },
      {
        provide: BrokerV2PanelService,
        useValue: {
          getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
          getCatalog: vi.fn().mockResolvedValue([]),
          previewStartAdmission: vi.fn(),
          deployBot: vi.fn(),
        },
      },
    ],
  });
}

describe('AlpacaDeployTabComponent', () => {
  it('preserves the edited form on directory refresh and exposes a later account read failure', async () => {
    const lane = testLane({ clerk_id: 'clrk_deploy_tab', effective_binding_generation: 3, routing_epoch: 7 });
    const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [lane] });
    const getAccount = vi.fn().mockResolvedValue(fakeAccount());
    const { fixture } = await render(AlpacaDeployTabComponent, {
      providers: [
        provideRouter([]), activatedRoute('clrk_deploy_tab'), directory, AlpacaDeskAccountDataService,
        ...fakePickerWorld().providers,
        { provide: CurrentUrlService, useValue: { url: signal('/brokers/alpaca/clerks/clrk_deploy_tab/accounts/pa9/deploy') } },
        { provide: BrokersService, useValue: { getAccount, getClerkStatus: vi.fn().mockResolvedValue({ account_id: 'PA9' }) } },
        { provide: BrokerV2PanelService, useValue: {
          getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW), getCatalog: vi.fn().mockResolvedValue([]),
        } },
      ],
    });
    await screen.findByRole('heading', { name: 'Bot binding' });
    fireEvent.input(screen.getByLabelText('Bot name'), { target: { value: 'my-edited-bot' } });
    directory.rebind({ observed_at_ms: 2, clerks: [{ ...lane }] });
    await directory.useValue.refresh?.();
    fixture.detectChanges();
    await fixture.whenStable();
    expect((screen.getByLabelText('Bot name') as HTMLInputElement).value).toBe('my-edited-bot');
    expect(getAccount).toHaveBeenCalledTimes(1);
    getAccount.mockRejectedValueOnce(new Error('Account unavailable'));
    TestBed.inject(AlpacaDeskAccountDataService).account.reload();
    fixture.detectChanges();
    expect(await screen.findByText('Alpaca has not confirmed this account yet.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Bot binding' })).toBeNull();
  });

  it('renews the opening fence only after a rejected attempt and explicit account review', async () => {
    const lane = testLane({ clerk_id: 'clrk_deploy_tab', effective_binding_generation: 3, routing_epoch: 7 });
    const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [lane] });
    const preview = vi.fn<BrokerV2PanelService['previewStartAdmission']>().mockRejectedValueOnce(new HttpErrorResponse({
      status: 409, error: { detail: { reason: 'clerk_binding_generation_conflict', message: 'Account binding changed.' } },
    })).mockResolvedValue({
      operation: 'START', allowed: false, reason_code: 'CLERK_HOLD_ACTIVE',
      explanation: 'Preview reached the reviewed account.', next_step: null,
      strategy_instance_id: 'reviewed-retry', proposed_run_id: 'run-2', configuration_hash: 'a'.repeat(64),
      account_id: 'PA9', evaluated_at_ms: 1_700_000_000_000,
      fact_ages_ms: { runtime: 5, process: 10, market_data: 20, market_liveness: 20, clerk: 30, program_build: 15 },
      evidence_refs: ['reviewed-account-preview'],
    });
    const { fixture } = await render(AlpacaDeployTabComponent, {
      providers: [
        provideRouter([]), activatedRoute('clrk_deploy_tab'), directory, AlpacaDeskAccountDataService,
        ...fakePickerWorld().providers,
        { provide: CurrentUrlService, useValue: { url: signal('/brokers/alpaca/clerks/clrk_deploy_tab/accounts/pa9/deploy') } },
        { provide: BrokersService, useValue: { getAccount: vi.fn().mockResolvedValue(fakeAccount()), getClerkStatus: vi.fn().mockResolvedValue({ account_id: 'PA9' }) } },
        { provide: BrokerV2PanelService, useValue: {
          getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW), getCatalog: vi.fn().mockResolvedValue([]), previewStartAdmission: preview,
        } },
      ],
    });
    await screen.findByRole('heading', { name: 'Bot binding' });
    fireEvent.input(screen.getByLabelText('Bot name'), { target: { value: 'reviewed-retry' } });
    directory.rebind({ observed_at_ms: 2, clerks: [{ ...lane, effective_binding_generation: 4, routing_epoch: 8 }] });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy paper bot' }));
    await vi.waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    const desk = TestBed.inject(AlpacaDeskAccountDataService);
    await vi.waitFor(() => expect(desk.target()?.bindingGeneration).toBe(4));
    expect(desk.fence()).toEqual({ bindingGeneration: 3, routingEpoch: 7 });
    expect(preview.mock.calls[0][0].bindingGeneration).toBe(3);
    expect((await screen.findByRole('button', { name: 'Deploy paper bot' }) as HTMLButtonElement).disabled).toBe(true);
    const review = await screen.findByRole('button', { name: 'Review refreshed account' });
    await vi.waitFor(() => expect((review as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(review);
    fixture.detectChanges();
    await fixture.whenStable();
    fireEvent.input(screen.getByLabelText('Bot name'), { target: { value: 'reviewed-retry' } });
    const deploy = screen.getByRole('button', { name: 'Deploy paper bot' });
    await vi.waitFor(() => expect((deploy as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(deploy);
    await vi.waitFor(() => expect(preview).toHaveBeenCalledTimes(2));
    expect(preview.mock.calls[1][0]).toMatchObject({ bindingGeneration: 4, routingEpoch: 8 });
    expect(preview.mock.calls[1][0].idempotencyKey).not.toBe(preview.mock.calls[0][0].idempotencyKey);
    expect(await screen.findAllByText('Preview reached the reviewed account.')).toHaveLength(2);
  });

  it('hosts the deploy workflow inline when the lane declares deploy capability and the account is confirmed', async () => {
    await renderDeployTab();

    expect(await screen.findByRole('heading', { name: 'Bot binding' })).toBeTruthy();
  });

  it('explains in place when this clerk has no resolved lane', async () => {
    await renderDeployTab({ lane: null });

    expect(await screen.findByText('Deploy is unavailable.')).toBeTruthy();
    expect(
      screen.getByText('This account’s lane has not resolved, so Deploy has no clerk to target.'),
    ).toBeTruthy();
  });

  it('explains in place when the lane does not declare deploy capability', async () => {
    await renderDeployTab({ lane: { capabilities: ['account_read', 'bot_panel_read'] } });

    expect(await screen.findByText('Deploy is unavailable.')).toBeTruthy();
    expect(screen.getByText('This clerk does not declare Deploy capability.')).toBeTruthy();
  });

  it('explains in place when Alpaca has not confirmed the account yet', async () => {
    await renderDeployTab({ hasAccount: false });

    expect(await screen.findByText('Deploy is unavailable.')).toBeTruthy();
    expect(screen.getByText('Alpaca has not confirmed this account yet.')).toBeTruthy();
  });
});
