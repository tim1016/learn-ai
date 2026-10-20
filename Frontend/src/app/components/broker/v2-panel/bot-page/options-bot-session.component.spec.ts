import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../../fleet/resource-target';
import { goldenSetup } from '../../../options-backtest/testing/golden-fixture';
import { summaryFor } from '../../../options-backtest/testing/fixtures';
import { OptionsBacktestService } from '../../../options-backtest/options-backtest.service';
import { GoldenOptionSetupsService } from '../../../options-backtest/golden-option-setups.service';
import { OptionsBotDeployService, type OptionsBotConfiguration, type OptionsBotSession } from '../../broker-deploy-page/options-bot-deploy.service';
import type { DeploymentBudgetInput, DeploymentBudgetPreview } from '../lib/broker-v2-panel.service';
import { OptionsBotSessionComponent } from './options-bot-session.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4, entityId: 'spy-options-1' });
const CONFIGURATION: OptionsBotConfiguration = {
  golden_setup_id: goldenSetup().id, setup: goldenSetup().setup,
  setup_sha256: goldenSetup().setup_sha256, rules_digest: goldenSetup().rules_digest,
};
const SESSION: OptionsBotSession = {
  strategy_instance_id: 'spy-options-1', golden_setup_id: goldenSetup().id,
  session_open_ms: 1791826200000, session_close_ms: 1791849600000, last_minute_ms: 1791828000000,
  pending_decisions: 0, entries_enabled: false, resume_allowed: true,
  resume_explanation: 'This interrupted bot can resume its retained session.', entry_hold_reason: 'New entries are disabled after interruption.',
  legs: [{ leg_id: 'call', side: 'buy', phase: 'held', entries_filled: 1, reentries_left: 1,
    held_contract: 'SPY261016C00500000', held_contracts: 1, average_fill_price: '2.50', stop_level: '2.00' }], events: [],
};

async function setup(state: OptionsBotSession = SESSION, configuration = CONFIGURATION) {
  const directory = provideFleetDirectory({ observed_at_ms: 1791828000000, clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 3, routing_epoch: 4 })] });
  const session = vi.fn().mockResolvedValue(state);
  const resume = vi.fn().mockResolvedValue({ running: true });
  const reviewResume = vi.fn().mockImplementation(async (_target, _sid, budget: DeploymentBudgetInput | null): Promise<DeploymentBudgetPreview> => ({
    state: 'ready', detail: 'The original free balance can be restored.', world: 'real_paper',
    budget_usd: '10000.00', risk_revision: 9, review_token: budget ? 'resume-review-9' : null,
    confirmation_text: null, shortcuts: [],
  }));
  const result = await render(OptionsBotSessionComponent, {
    inputs: { target: TARGET, sid: 'spy-options-1', configuration, revision: 1 },
    providers: [
      directory,
      { provide: OptionsBacktestService, useValue: { describe: async () => summaryFor('Tested setup') } },
      { provide: OptionsBotDeployService, useValue: { session, resume, reviewResume } },
      { provide: GoldenOptionSetupsService, useValue: { get: async () => goldenSetup() } },
    ],
  });
  await screen.findByText('This interrupted bot can resume its retained session.');
  return { ...result, session, resume, reviewResume, directory };
}

describe('Options session recovery', () => {
  it('explains when the retained proof belongs to a different sealed setup', async () => {
    await setup(SESSION, { ...CONFIGURATION, setup_sha256: '0'.repeat(64) });
    fireEvent.click(screen.getByRole('button', { name: 'Build proof' }));
    await screen.findByText('The retained proof does not match this deployment’s exact setup and rules.');
    expect(screen.queryByText(goldenSetup().status_explanation)).toBeNull();
  });
  it('says when saved premiums and leg P&L are unavailable', async () => {
    await setup({ ...SESSION, legs: [{ ...SESSION.legs[0], realized_pnl_usd: null, open_pnl_usd: null,
      watched_contracts: [{ symbol: 'SPY261016C00500000', premium: null, close_ms: null }],
      pnl_notice: 'The session’s attributed fills are unavailable.' }] });
    expect(screen.getByText(/Latest premium:.*Unavailable/)).toBeTruthy();
    expect(screen.getByText(/Realized P&L:.*Unavailable.*Open P&L:.*Unavailable/)).toBeTruthy();
    expect(screen.getByText('The session’s attributed fills are unavailable.')).toBeTruthy();
  });
  it('shows the waiting contract, trigger, premium and deployed terms, and opens retained proof', async () => {
    await setup({ ...SESSION,
      order_terms: { gap_limit_percent: '10', entry_wait_seconds: 20, cost_limit_percent: '5', expiry_close_lead_minutes: 45 },
      exit_terms: { band_multiple: 2, spread_cap_bps: 25, exit_allowance_bps: 15, provenance: 'deployed' },
      legs: [{ ...SESSION.legs[0], held_contract: null, held_contracts: 0, phase: 'waiting', waiting_contract: 'SPY261016C00500000',
        starting_price: '2', momentum_level: '2.50', watched_contracts: [{ symbol: 'SPY261016C00500000', premium: '2.30', close_ms: SESSION.last_minute_ms }],
        realized_pnl_usd: '0', open_pnl_usd: '0' }],
    });
    expect(screen.getByText(/Waiting on/)).toBeTruthy();
    expect(screen.getByText(/Momentum level:.*\$2.50/)).toBeTruthy();
    expect(screen.getByText(/Latest premium:.*\$2.30/)).toBeTruthy();
    expect(screen.getByText('20 seconds')).toBeTruthy();
    expect(screen.getByText('45 minutes')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Build proof' }));
    await screen.findByRole('heading', { name: 'Build proof' });
    await screen.findByText(goldenSetup().status_explanation);
  });
  it('refuses Resume when the lane moves beyond the inherited frozen target', async () => {
    const { directory, fixture, resume } = await setup();
    directory.rebind({ observed_at_ms: 1791828000001, clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4, routing_epoch: 5 })] });
    await directory.useValue.refresh?.();
    await fixture.whenStable();

    const button = screen.getByRole('button', { name: 'Resume position management' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(resume).not.toHaveBeenCalled();
    expect(screen.getByText(/lane was rebound while the action was open/)).toBeTruthy();
  });

  it('shows a pending close refusal and clears it when that decision settles', async () => {
    const reason = 'call: The close needs a fresh quote. The same close decision will be retried.';
    const { session, rerender } = await setup({ ...SESSION, pending_decisions: 1, execution_hold_reason: reason });
    expect(screen.getByText(reason)).toBeTruthy();
    session.mockResolvedValue(SESSION);
    await rerender({ inputs: { target: TARGET, sid: 'spy-options-1', configuration: CONFIGURATION, revision: 2 } });
    await waitFor(() => expect(screen.queryByText(reason)).toBeNull());
  });
  it('recovers a recorded stopped request without claiming the bot is running', async () => {
    const { resume } = await setup();
    resume.mockResolvedValueOnce({ running: false });
    fireEvent.click(screen.getByRole('button', { name: 'Resume position management' }));
    await screen.findByText('This recovery request is recorded. The bot is currently stopped; its status shows how it ended.');
    expect(screen.queryByText('Position management resumed. New entries and re-entries remain disabled.')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry the same Resume request' })).toBeNull();
  });
  it('shows confirmed leg state and resumes management without granting another entry budget', async () => {
    const { resume } = await setup();
    expect(screen.getByText(/Average confirmed fill:.*\$2.50/)).toBeTruthy();
    expect(screen.getByText(/Stop level:.*\$2.00/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Resume position management' }));
    await screen.findByText('Position management resumed. New entries and re-entries remain disabled.');
    const [target, sid, request] = resume.mock.calls[0];
    expect(sid).toBe('spy-options-1');
    expect(request.budget).toBeNull();
    expect(target.bindingGeneration).toBe(3);
    expect(target.idempotencyKey).toBe(request.idempotency_key);
  });

  it('reviews the retained budget and sends its exact consent before enabling entries', async () => {
    const { resume, reviewResume } = await setup();
    fireEvent.click(screen.getByRole('button', { name: 'Review budget for new entries' }));
    const button = await screen.findByRole('button', { name: 'Resume Options bot with entries' });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    await screen.findByText('The Options bot resumed with reviewed entry funding.');
    expect(reviewResume.mock.calls[1][2]).toEqual({ amount_usd: '10000.00', risk_revision: 9 });
    expect(resume.mock.calls[0][2].budget).toEqual({ amount_usd: '10000.00', risk_revision: 9,
      review_token: 'resume-review-9', live_confirmation: null });
  });

  it('retries the same frozen Resume request after its response is lost', async () => {
    const { resume } = await setup();
    resume.mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: { detail: { message: 'Resume response lost.' } } }));
    fireEvent.click(screen.getByRole('button', { name: 'Resume position management' }));
    const retry = await screen.findByRole('button', { name: 'Retry the same Resume request' });
    fireEvent.click(retry);
    await screen.findByText('Position management resumed. New entries and re-entries remain disabled.');
    expect(resume.mock.calls[0]).toEqual(resume.mock.calls[1]);
  });

  it('allows a fresh review only after a definitive refusal says Resume did not start', async () => {
    const { resume } = await setup();
    resume.mockRejectedValueOnce(new HttpErrorResponse({ status: 409, error: {
      detail: { code: 'OPTIONS_RESUME_NOT_STARTED', message: 'Risk limits changed. Review the account again.' },
    } }));
    fireEvent.click(screen.getByRole('button', { name: 'Resume position management' }));
    await screen.findByText('Risk limits changed. Review the account again.');
    expect(screen.queryByRole('button', { name: 'Retry the same Resume request' })).toBeNull();
    const resumeAgain = screen.getByRole('button', { name: 'Resume position management' });
    await waitFor(() => expect((resumeAgain as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(resumeAgain);
    await screen.findByText('Position management resumed. New entries and re-entries remain disabled.');
    expect(resume.mock.calls[0][2].idempotency_key).not.toBe(resume.mock.calls[1][2].idempotency_key);
  });
});
