import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter, Router } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../../shared/date/et-midnight';
import { localWallClock } from '../../../shared/date/local-wall-clock';
import { provideFakeDateWorld } from '../../../shared/date-picker/testing/fake-trading-calendar';
import { resourceTarget } from '../../../fleet/resource-target';
import { GoldenOptionSetupsService, type GoldenOptionSetup } from '../../options-backtest/golden-option-setups.service';
import { OptionsBacktestService } from '../../options-backtest/options-backtest.service';
import { exceptionGoldenSetup, goldenSetup } from '../../options-backtest/testing/golden-fixture';
import { summaryFor } from '../../options-backtest/testing/fixtures';
import { PositionPreviewService } from '../../options-backtest/position/position-preview.service';
import { previewAnswer } from '../../options-backtest/position/testing';
import { BrokerV2PanelService, type BudgetDeployReceipt, type BotEndPreviewRequest } from '../v2-panel/lib/broker-v2-panel.service';
import { DEPLOY_VIEW, LIVE_DEPLOY_VIEW } from './alpaca-deploy-workflow.fixtures';
import { OPTION_CATALOGUE } from './option-structure/option-structure.fixtures';
import { OptionsBotDeployComponent } from './options-bot-deploy.component';
import { OptionsBotDeployService, type OptionsDeploymentReview, type OptionsDeployRequest } from './options-bot-deploy.service';

const DEFAULT_OPTION_END_MS = Date.UTC(2026, 9, 20, 19, 15);
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

async function setup(options: { live?: boolean; selectedId?: string; versions?: GoldenOptionSetup[]; noExitDefaults?: boolean; endReadFails?: boolean } = {}) {
  const review = vi.fn().mockImplementation(async (_target, request: OptionsDeployRequest) => {
    const result = reviewed(request);
    return options.live ? { ...result, budget: { ...result.budget, world: 'real_live', confirmation_text: 'DEPLOY PA9 $10000.00' } } : result;
  });
  const deploy = vi.fn().mockResolvedValue(RECEIPT);
  const submission = vi.fn().mockResolvedValue(RECEIPT);
  const previewBotEnd = vi.fn().mockImplementation(async (_target, body: BotEndPreviewRequest) => {
    if (options.endReadFails && body.end === undefined) {
      throw new HttpErrorResponse({ status: 503, error: { detail: { message: 'The calendar end could not be read.' } } });
    }
    return { ...DEPLOY_VIEW.default_end, ...(body.end ?? { end_at_ms: DEFAULT_OPTION_END_MS, end_action: 'SELL' }),
      status: body.end?.end_at_ms === null ? 'no_end' : 'scheduled', headline: 'Reviewed bot end' };
  });
  const get = vi.fn().mockResolvedValue(goldenSetup());
  const preview = vi.fn().mockResolvedValue(previewAnswer());
  const getDeployView = vi.fn().mockResolvedValue({ ...(options.live ? LIVE_DEPLOY_VIEW : DEPLOY_VIEW),
    ...(options.noExitDefaults ? { default_exit_terms: null } : {}) });
  const result = await render(OptionsBotDeployComponent, {
    inputs: { target: TARGET, accountId: 'PA9', fence: { bindingGeneration: 3, routingEpoch: 4 } },
    providers: [
      provideRouter([]),
      ...provideFakeDateWorld(),
      { provide: ActivatedRoute, useValue: { snapshot: { queryParamMap: convertToParamMap(options.selectedId ? { options_setup: options.selectedId } : {}) } } },
      { provide: GoldenOptionSetupsService, useValue: { list: async () => options.versions ?? [goldenSetup()], get } },
      { provide: OptionsBacktestService, useValue: { describe: async () => summaryFor('Tested setup') } },
      { provide: PositionPreviewService, useValue: { preview } },
      { provide: BrokerV2PanelService, useValue: { getDeployView, previewBotEnd, getOptionStructures: async () => OPTION_CATALOGUE } },
      { provide: OptionsBotDeployService, useValue: { review, deploy, submission } },
    ],
  });
  vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  if (!options.selectedId) await userEvent.selectOptions(await screen.findByLabelText('Approved tested version'), goldenSetup().id);
  await waitFor(() => expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('10000'));
  if (!options.endReadFails) await waitFor(() => expect(screen.getByLabelText<HTMLInputElement>('End date').value).not.toBe(''));
  return { ...result, review, deploy, submission, get, preview, previewBotEnd, getDeployView };
}

async function reviewSetup(): Promise<void> {
  await waitFor(() => expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Review setup and budget' }));
  await screen.findByRole('button', { name: 'Deploy Options bot' });
}

describe('Options deployment', () => {
  it('pre-fills the server’s calendar-owned end and submits it without opting into no end', async () => {
    const { review, previewBotEnd } = await setup();
    const wall = localWallClock(DEFAULT_OPTION_END_MS);
    expect(screen.getByLabelText<HTMLInputElement>('End date').value).toBe(wall.date);
    expect(screen.getByLabelText<HTMLInputElement>(/^End time/).value).toBe(wall.clock);
    expect(screen.getByRole<HTMLInputElement>('checkbox', { name: 'No end — run until I stop it' }).checked).toBe(false);
    expect(previewBotEnd).toHaveBeenCalledWith(expect.objectContaining({ accountId: 'PA9' }), {
      execution_mode: 'paper', bot_kind: 'options',
    });
    await reviewSetup();
    expect(review.mock.calls[1][1].end).toEqual({ end_at_ms: DEFAULT_OPTION_END_MS, end_action: 'SELL' });
  });

  it('preserves a changed deployment date and time when the account is refreshed', async () => {
    const { fixture, previewBotEnd } = await setup();
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '2026-10-22' } });
    fireEvent.input(screen.getByLabelText(/^End time/), { target: { value: '12:30' } });
    fixture.componentRef.setInput('fence', { bindingGeneration: 3, routingEpoch: 5 });
    await waitFor(() => expect(previewBotEnd).toHaveBeenCalledTimes(2));
    await fixture.whenStable();
    expect(screen.getByLabelText<HTMLInputElement>('End date').value).toBe('2026-10-22');
    expect(screen.getByLabelText<HTMLInputElement>(/^End time/).value).toBe('12:30');
  });

  it('requires a readable calendar default and offers a retry when it fails', async () => {
    const { previewBotEnd, review } = await setup({ endReadFails: true });
    await screen.findByText('The calendar end could not be read.');
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Review setup and budget' }).disabled).toBe(true);
    expect(review).not.toHaveBeenCalled();
    previewBotEnd.mockResolvedValue({ ...DEPLOY_VIEW.default_end, end_at_ms: DEFAULT_OPTION_END_MS });
    fireEvent.click(screen.getByRole('button', { name: 'Read settings again' }));
    await waitFor(() => expect(screen.getByLabelText<HTMLInputElement>('End date').value).not.toBe(''));
    expect(screen.queryByText('The calendar end could not be read.')).toBeNull();
  });

  it.each([false, true])('pre-fills all three account exit defaults and reviews them (live: %s)', async (live) => {
    const { review } = await setup({ live });
    expect(screen.getByLabelText<HTMLInputElement>('Exit allowance (bps)').value).toBe('20');
    expect(screen.getByLabelText<HTMLInputElement>('Band multiple').value).toBe('2');
    expect(screen.getByLabelText<HTMLInputElement>('Spread cap (bps)').value).toBe('50');
    if (live) await userEvent.selectOptions(screen.getByLabelText('Execution mode'), 'live');
    await reviewSetup();
    expect(review.mock.calls[1][1].exit_terms).toEqual(DEPLOY_VIEW.default_exit_terms);
  });

  it('inherits newly available defaults on a refreshed account read and keeps subsequent edits', async () => {
    const { fixture, getDeployView } = await setup({ noExitDefaults: true });
    expect(screen.getByLabelText<HTMLInputElement>('Exit allowance (bps)').value).toBe('');
    getDeployView.mockResolvedValue(DEPLOY_VIEW);
    fixture.componentRef.setInput('fence', { bindingGeneration: 3, routingEpoch: 5 });
    await waitFor(() => expect(screen.getByLabelText<HTMLInputElement>('Exit allowance (bps)').value).toBe('20'));
    expect(screen.getByLabelText<HTMLInputElement>('Band multiple').value).toBe('2');
    expect(screen.getByLabelText<HTMLInputElement>('Spread cap (bps)').value).toBe('50');

    fireEvent.input(screen.getByLabelText('Exit allowance (bps)'), { target: { value: '0' } });
    fireEvent.input(screen.getByLabelText('Band multiple'), { target: { value: '3' } });
    fireEvent.input(screen.getByLabelText('Spread cap (bps)'), { target: { value: '' } });
    fixture.componentRef.setInput('fence', { bindingGeneration: 3, routingEpoch: 6 });
    await fixture.whenStable();
    expect(screen.getByLabelText<HTMLInputElement>('Exit allowance (bps)').value).toBe('0');
    expect(screen.getByLabelText<HTMLInputElement>('Band multiple').value).toBe('3');
    expect(screen.getByLabelText<HTMLInputElement>('Spread cap (bps)').value).toBe('');
  });

  it('shows a server-authored position preview for the selected approved setup', async () => {
    const { preview } = await setup();
    await screen.findByRole('heading', { name: 'Historical position payoff' });
    await screen.findByText('Underlying $500.00');
    expect(screen.getByText('Option order terms').closest('details')?.open).toBe(true);
    expect(screen.getByText('Per-bot exit settings').closest('details')?.open).toBe(true);
    expect(preview).toHaveBeenCalledWith(expect.objectContaining({
      setup: goldenSetup().setup, source: 'historical', mode: 'position', trading_date_ms: etMidnightMs('2026-10-09'),
    }));
    expect(screen.getByText('Resolved contracts and price evidence')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Current market' })).toBeNull();
    await reviewSetup();
  });

  it('reviews and deploys explicit per-bot terms and a bounded end when account defaults are absent', async () => {
    const { review, deploy, previewBotEnd } = await setup({ noExitDefaults: true });
    expect(screen.getByRole('link', { name: 'Set defaults for new bots in Settings' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/settings');
    expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText('Per-bot exit settings'));
    fireEvent.input(screen.getByLabelText('Exit allowance (bps)'), { target: { value: '20' } });
    fireEvent.input(screen.getByLabelText('Band multiple'), { target: { value: '2' } });
    fireEvent.input(screen.getByLabelText('Spread cap (bps)'), { target: { value: '50' } });
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '5000' } });
    const endAtMs = 1792439940000;
    const wall = localWallClock(endAtMs);
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: wall.date } });
    fireEvent.input(screen.getByLabelText(/^End time/), { target: { value: wall.clock } });
    await reviewSetup();
    expect(previewBotEnd).toHaveBeenCalledWith(expect.objectContaining({ accountId: 'PA9' }), {
      execution_mode: 'paper', bot_kind: 'options', end: { end_at_ms: endAtMs, end_action: 'SELL' },
    });
    expect(review.mock.calls[1][1]).toMatchObject({
      exit_terms: { exit_allowance_bps: 20, band_multiple: 2, spread_cap_bps: 50 },
      end: { end_at_ms: endAtMs, end_action: 'SELL' }, budget: { amount_usd: '5000', risk_revision: 8 },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy Options bot' }));
    await screen.findByRole('heading', { name: 'Options bot is deployed' });
    expect(deploy.mock.calls[0][1]).toMatchObject({
      exit_terms: review.mock.calls[1][1].exit_terms, end: review.mock.calls[1][1].end,
      budget: { amount_usd: '5000', review_token: 'review-options-8' },
    });
  });

  it('invalidates consent for changed exit terms or an incomplete end', async () => {
    await setup();
    await reviewSetup();
    fireEvent.click(screen.getByText('Per-bot exit settings'));
    fireEvent.input(screen.getByLabelText('Band multiple'), { target: { value: '0.5' } });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Deploy Options bot' })).toBeNull());
    expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.input(screen.getByLabelText('Band multiple'), { target: { value: '3' } });
    await reviewSetup();
    fireEvent.change(screen.getByLabelText('End date'), { target: { value: '' } });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Deploy Options bot' })).toBeNull());
    expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows an end refusal before budget review and never offers a deployment', async () => {
    const { review, deploy, previewBotEnd } = await setup();
    previewBotEnd.mockRejectedValueOnce(new HttpErrorResponse({ status: 400, error: { detail: { message: 'This end has passed.' } } }));
    fireEvent.click(screen.getByRole('button', { name: 'Review setup and budget' }));
    await screen.findByText('This end has passed.');
    expect(review).not.toHaveBeenCalled();
    expect(deploy).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Deploy Options bot' })).toBeNull();
  });

  it('keeps the permanent owner exception visible in the chooser and deployment review', async () => {
    await setup({ versions: [exceptionGoldenSetup()] });
    expect(screen.getByRole('option', { name: /Owner exception/ })).toBeTruthy();
    expect(screen.getByText('Owner exception', { exact: true })).toBeTruthy();
    expect(screen.getByText(/The final Sharpe hurdle was not met/)).toBeTruthy();
    expect(screen.getByText(/The final interval was used in recorded research/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Review source Options study' })).toBeTruthy();
  });

  it('labels a stale selected version and keeps deployment review closed', async () => {
    const stale = goldenSetup({ status: 'stale', status_explanation: 'The proof needs refreshing.' });
    await setup({ selectedId: stale.id, versions: [stale] });
    expect(screen.getByText('Stale', { exact: true })).toBeTruthy();
    expect(screen.getByText('The proof needs refreshing.')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Review setup and budget' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('loads an older selected approval even when the recent list does not contain it', async () => {
    const { get } = await setup({ selectedId: goldenSetup().id, versions: [] });
    await reviewSetup();
    expect(get).toHaveBeenCalledWith(goldenSetup().id);
    expect((screen.getByLabelText('Approved tested version') as HTMLSelectElement).value).toBe(goldenSetup().id);
    expect((screen.getByRole('button', { name: 'Deploy Options bot' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('requires fresh typed Live consent after changed order terms are reviewed', async () => {
    await setup({ live: true });
    await userEvent.selectOptions(screen.getByLabelText('Execution mode'), 'live');
    await reviewSetup();
    await userEvent.type(screen.getByLabelText('Type DEPLOY PA9 $10000.00 to confirm'), 'DEPLOY PA9 $10000.00');
    await waitFor(() => expect((screen.getByRole('button', { name: 'Deploy Options bot' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.input(screen.getByLabelText(/Entry wait/), { target: { value: '25' } });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Deploy Options bot' })).toBeNull());

    await reviewSetup();

    expect((screen.getByLabelText('Type DEPLOY PA9 $10000.00 to confirm') as HTMLInputElement).value).toBe('');
    expect((screen.getByRole('button', { name: 'Deploy Options bot' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('reviews current risk revision and deploys the exact Golden version with the frozen account fence', async () => {
    const { review, deploy } = await setup();
    await reviewSetup();
    expect(review.mock.calls[0][1].budget).toBeNull();
    expect(review.mock.calls[1][1].budget).toEqual({ amount_usd: '10000', risk_revision: 8 });
    fireEvent.click(screen.getByRole('button', { name: 'Deploy Options bot' }));
    await screen.findByRole('heading', { name: 'Options bot is deployed' });

    const [target, request] = deploy.mock.calls[0];
    expect(target).toMatchObject({ accountId: 'PA9', clerkId: 'clrk_spec', bindingGeneration: 3, routingEpoch: 4 });
    expect(request).toMatchObject({ golden_setup_id: goldenSetup().id, execution_mode: 'paper', end: { end_at_ms: DEFAULT_OPTION_END_MS, end_action: 'SELL' },
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
    expect((screen.getByRole('checkbox', { name: 'No end — run until I stop it' }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText('Band multiple') as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(recover);
    await screen.findByRole('heading', { name: 'Options bot is deployed' });
    expect(deploy).toHaveBeenCalledTimes(1);
    expect(submission.mock.calls[0][1]).toBe(deploy.mock.calls[0][1].submission_key);
  });
});
