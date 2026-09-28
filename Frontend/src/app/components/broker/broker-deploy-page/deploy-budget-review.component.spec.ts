import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { resourceTarget } from '../../../fleet/resource-target';
import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { BrokerV2PanelService, type DeployBotBody, type DeploymentBudgetPreview } from '../v2-panel/lib/broker-v2-panel.service';
import { DeployBudgetReviewComponent } from './deploy-budget-review.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });
const BODY: DeployBotBody = {
  strategy_instance_id: 'budget-bot', strategy_key: 'deployment_validation', symbol: 'SPY',
  execution_mode: 'paper', sizing: { preset: 'safe_canary', quantity: 1 }, carryover_policy: 'FORBID',
  exit_terms: { exit_allowance_bps: 10, band_multiple: 2, spread_cap_bps: 10 }, parameters: {},
};
const FACTS: DeploymentBudgetPreview = {
  state: 'ready', detail: 'Current account evidence is complete.', world: 'real_paper', custody_account_id: 'PA9',
  risk_revision: 4, unreserved_usd: '1234.56', minimum_budget_usd: '501.02', estimated_price_usd: '501.01',
  shortcuts: [{ key: 'half', label: 'Half of unreserved cash', amount_usd: '617.28', explanation: '50% of the observed $1,234.56.' }],
  review_token: null, confirmation_text: null, observed_at_ms: 1_800_000_000_000,
};

async function setup(confirmation: string | null = null) {
  const previewBudget = vi.fn().mockImplementation(async (_target, body: DeployBotBody) => ({
    ...FACTS, confirmation_text: body.budget ? confirmation : null, review_token: body.budget ? 'review-1' : null,
  }));
  const reviewed = vi.fn();
  const result = await render(DeployBudgetReviewComponent, { inputs: { target: TARGET, body: BODY },
    on: { reviewed }, providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }] });
  await screen.findByText('Current account evidence is complete.');
  return { ...result, previewBudget, reviewed };
}

async function reviewAmount(amount = '617.28') {
  fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: amount } });
  fireEvent.click(screen.getByRole('button', { name: 'Review budget' }));
  await screen.findByText(/Reviewed budget:/);
}

const PRICE_WAIT: DeploymentBudgetPreview = {
  state: 'awaiting_price', detail: 'Wait for a fresh IBKR price for this instrument, then review the budget.',
  world: 'real_paper', custody_account_id: 'PA9',
};

async function renderPriceWait(previewBudget: ReturnType<typeof vi.fn>) {
  vi.useFakeTimers();
  await render(DeployBudgetReviewComponent, { inputs: { target: TARGET, body: BODY },
    providers: [{ provide: BrokerV2PanelService, useValue: { previewBudget } }] });
  await vi.advanceTimersByTimeAsync(0);
}

describe('deployment budget review', () => {
  afterEach(() => vi.useRealTimers());

  it('copies the server shortcut and keeps dollars fixed through refreshed cash evidence', async () => {
    const { previewBudget, reviewed } = await setup();
    fireEvent.click(screen.getByRole('button', { name: 'Half of unreserved cash · $617.28' }));
    await reviewAmount();
    expect(previewBudget.mock.calls.at(-1)?.[1].budget.amount_usd).toBe('617.28');
    expect(reviewed.mock.calls.at(-1)?.[0].budget.review_token).toBe('review-1');
    previewBudget.mockResolvedValue({ ...FACTS, unreserved_usd: '1000.00' });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh money' }));
    await screen.findByText('$1,000.00');
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('617.28');
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('requires typed Live consent and invalidates it for a material change', async () => {
    const { fixture, reviewed } = await setup('LIVE PA9 budget-bot 617.28');
    await reviewAmount();
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
    const confirmation = screen.getByLabelText(/Confirm Live deployment by typing/);
    fireEvent.input(confirmation, { target: { value: 'LIVE PA9 budget-bot 617.28' } });
    await vi.waitFor(() => expect(reviewed.mock.calls.at(-1)?.[0]?.budget.live_confirmation).toBe('LIVE PA9 budget-bot 617.28'));
    fixture.componentRef.setInput('body', { ...BODY, exit_terms: { ...BODY.exit_terms, band_multiple: 3 } });
    await fixture.whenStable();
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
    expect(screen.queryByLabelText(/Confirm Live deployment/)).toBeNull();
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('617.28');
  });

  it('cannot revive old consent by changing dollars away and back', async () => {
    const { reviewed } = await setup();
    await reviewAmount();
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '700.00' } });
    await vi.waitFor(() => expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull());
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });
    expect(screen.queryByText(/Reviewed budget:/)).toBeNull();
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('rejects a late preview for the prior configuration', async () => {
    const { fixture, previewBudget, reviewed } = await setup();
    let resolve!: (value: DeploymentBudgetPreview) => void;
    previewBudget.mockImplementationOnce(() => new Promise<DeploymentBudgetPreview>(done => { resolve = done; }));
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review budget' }));
    fixture.componentRef.setInput('body', { ...BODY, symbol: 'QQQ' });
    await fixture.whenStable();
    resolve({ ...FACTS, review_token: 'old-configuration' });
    await fixture.whenStable();
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
    expect(screen.queryByText(/Reviewed budget:/)).toBeNull();
  });
  it('renders a fleet-authored refusal without changing the chosen dollar amount', async () => {
    const { previewBudget, reviewed } = await setup();
    previewBudget.mockRejectedValueOnce(new HttpErrorResponse({ status: 409,
      error: { reason: 'budget_review_changed', message: 'Risk limits changed. Refresh and review these dollars again.' } }));
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '617.28' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review budget' }));
    await screen.findByText('Risk limits changed. Refresh and review these dollars again.');
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('617.28');
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
  });

  it('says an amount the backend refused as a refusal, and never counts it as reviewed', async () => {
    const { previewBudget, reviewed } = await setup();
    previewBudget.mockResolvedValueOnce({
      ...FACTS, state: 'unavailable', detail: 'Only $1234.56 is unreserved. Choose a smaller budget or resolve existing claims.',
      review_token: null, money_after: fakeAccountMoney(),
    });
    fireEvent.input(screen.getByLabelText('Dollar budget (USD)'), { target: { value: '5000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review budget' }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('Only $1234.56 is unreserved. Choose a smaller budget or resolve existing claims.');
    expect(screen.queryByText(/Reviewed budget:/)).toBeNull();
    expect(reviewed.mock.calls.at(-1)?.[0]).toBeNull();
    expect((screen.getByLabelText('Dollar budget (USD)') as HTMLInputElement).value).toBe('5000');
  });

  it('re-checks a price wait on its own until the IBKR quote arrives, then stops', async () => {
    const previewBudget = vi.fn().mockResolvedValueOnce(PRICE_WAIT).mockResolvedValueOnce(PRICE_WAIT).mockResolvedValue(FACTS);
    await renderPriceWait(previewBudget);
    expect(screen.getByText(PRICE_WAIT.detail)).toBeTruthy();

    await vi.advanceTimersByTimeAsync(60_000);

    expect(screen.getByText('Current account evidence is complete.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Half of unreserved cash · $617.28' })).toBeTruthy();
    expect(previewBudget).toHaveBeenCalledTimes(3);
  });

  it('bounds the automatic price re-checks and leaves the manual refresh', async () => {
    const previewBudget = vi.fn().mockResolvedValue(PRICE_WAIT);
    await renderPriceWait(previewBudget);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    const bounded = previewBudget.mock.calls.length;
    expect(bounded).toBeGreaterThan(1);

    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(previewBudget).toHaveBeenCalledTimes(bounded);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh money' }));
    await vi.advanceTimersByTimeAsync(0);
    expect(previewBudget).toHaveBeenCalledTimes(bounded + 1);
    expect(screen.getByText(PRICE_WAIT.detail)).toBeTruthy();
  });
});
