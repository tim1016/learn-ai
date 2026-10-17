import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { resourceTarget } from '../../../fleet/resource-target';
import { GoldenOptionSetupsService } from '../../options-backtest/golden-option-setups.service';
import { OptionsBacktestService } from '../../options-backtest/options-backtest.service';
import { goldenSetup } from '../../options-backtest/testing/golden-fixture';
import { summaryFor } from '../../options-backtest/testing/fixtures';
import { BrokerV2PanelService, type BudgetDeployReceipt } from '../v2-panel/lib/broker-v2-panel.service';
import { DEPLOY_VIEW } from './alpaca-deploy-workflow.fixtures';
import { OPTION_CATALOGUE } from './option-structure/option-structure.fixtures';
import { OptionsBotDeployComponent } from './options-bot-deploy.component';
import { OptionsBotDeployService, type OptionsDeploymentReview, type OptionsDeployRequest } from './options-bot-deploy.service';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const RECEIPT: BudgetDeployReceipt = {
  outcome: 'success', status: 'deployed', receipt_id: 'receipt-options-1', recorded_at_ms: 1791828000000,
  first_deployed_at_ms: 1791828000000, account_id: 'PA9', command_id: 'command-options-1', committed_usd: '10000.00',
  strategy_instance_id: 'spy-options-1', run_id: 'run-options-1', world: 'real_paper',
  message: 'Options bot is deployed', explanation: 'The tested setup is registered.', next_action: 'Open the bot.',
};

function reviewed(request: OptionsDeployRequest): OptionsDeploymentReview {
  return {
    golden: goldenSetup(), allowed: request.budget != null, explanation: 'The exact setup and account were reviewed.',
    rules_summary: ['Contract quantities are fixed.'], first_eligible_session_open_ms: 1791898200000,
    budget: {
      state: 'ready', detail: 'The entry budget can be set aside.', world: 'real_paper', risk_revision: 8,
      budget_usd: request.budget?.amount_usd ?? null, review_token: request.budget ? 'review-options-8' : null,
      shortcuts: [], confirmation_text: null,
    },
  };
}

async function setup() {
  const review = vi.fn().mockImplementation(async (_target, request: OptionsDeployRequest) => reviewed(request));
  const deploy = vi.fn().mockResolvedValue(RECEIPT);
  const submission = vi.fn().mockResolvedValue(RECEIPT);
  const result = await render(OptionsBotDeployComponent, {
    inputs: { target: TARGET, accountId: 'PA9', fence: { bindingGeneration: 3, routingEpoch: 4 } },
    providers: [
      provideRouter([]),
      { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap({}) } } },
      { provide: GoldenOptionSetupsService, useValue: { list: async () => [goldenSetup()] } },
      { provide: OptionsBacktestService, useValue: { describe: async () => summaryFor('Tested setup') } },
      { provide: BrokerV2PanelService, useValue: { getDeployView: async () => DEPLOY_VIEW, getOptionStructures: async () => OPTION_CATALOGUE } },
      { provide: OptionsBotDeployService, useValue: { review, deploy, submission } },
    ],
  });
  vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  await userEvent.selectOptions(await screen.findByLabelText('Approved tested version'), goldenSetup().id);
  await waitFor(() => expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('10000'));
  return { ...result, review, deploy, submission };
}

async function reviewSetup(): Promise<void> {
  await waitFor(() => expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Review setup and budget' }));
  await screen.findByRole('button', { name: 'Deploy Options bot' });
}

describe('Options deployment', () => {
  it('reviews current risk revision and deploys the exact Golden version with the frozen account fence', async () => {
    const { review, deploy } = await setup();
    await reviewSetup();
    expect(review.mock.calls[0][1].budget).toBeNull();
    expect(review.mock.calls[1][1].budget).toEqual({ amount_usd: '10000', risk_revision: 8 });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy Options bot' }));
    await screen.findByRole('heading', { name: 'Options bot is deployed' });

    const [target, request] = deploy.mock.calls[0];
    expect(target).toMatchObject({ accountId: 'PA9', clerkId: 'clrk_spec', bindingGeneration: 3, routingEpoch: 4 });
    expect(request).toMatchObject({ golden_setup_id: goldenSetup().id, execution_mode: 'paper', end: null,
      budget: { amount_usd: '10000', risk_revision: 8, review_token: 'review-options-8' } });
    expect(target.idempotencyKey).toBe(request.submission_key);
    expect(request).not.toHaveProperty('quantity');
    expect(request).not.toHaveProperty('strategy_key');
  });

  it('invalidates reviewed consent when the amount changes or the lane needs review', async () => {
    const { fixture, deploy } = await setup();
    await reviewSetup();
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '9000' } });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Deploy Options bot' })).toBeNull());
    fixture.componentRef.setInput('laneReviewRequired', true);
    await fixture.whenStable();
    expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(true);
    expect(deploy).not.toHaveBeenCalled();
  });

  it('holds a lost submission until its status read recovers the same deployment key', async () => {
    const { deploy, submission } = await setup();
    deploy.mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: { detail: { message: 'Response lost.' } } }));
    await reviewSetup();
    fireEvent.click(screen.getByRole('button', { name: 'Deploy Options bot' }));
    const recover = await screen.findByRole('button', { name: 'Check deployment status' });
    await waitFor(() => expect((recover as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(recover);
    await screen.findByRole('heading', { name: 'Options bot is deployed' });
    expect(deploy).toHaveBeenCalledTimes(1);
    expect(submission.mock.calls[0][1]).toBe(deploy.mock.calls[0][1].submission_key);
  });
});
