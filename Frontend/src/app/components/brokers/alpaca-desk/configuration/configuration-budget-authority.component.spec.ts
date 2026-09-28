import axe from 'axe-core';
import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { BrokerConfigurationService } from './broker-configuration.service';
import { ConfigurationBudgetAuthorityComponent } from './configuration-budget-authority.component';

const LEGACY = { state: 'legacy', account_id: 'PA9', authorization_version: 1, active_run_count: 2,
  review_token: 'review-old', detail: 'Review the switch to dollar budgets.' };
const BUDGET = { ...LEGACY, state: 'budget', authorization_version: 2, active_run_count: 0,
  review_token: 'review-new', detail: 'Budget authority is now effective. Existing obligations remain.' };

const OPEN = 'Switch to budgets…';
const CONFIRM = 'Stop bots and switch to budgets';

async function setup(initial: typeof LEGACY = LEGACY) {
  const lane = testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 3, routing_epoch: 4 });
  const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [lane] });
  const service = { readBudgetAuthority: vi.fn().mockResolvedValue(initial), applyBudgetAuthority: vi.fn().mockResolvedValue(BUDGET) };
  const result = await render(ConfigurationBudgetAuthorityComponent, { inputs: { clerkId: 'clrk_spec', accountId: 'PA9' },
    providers: [directory, { provide: BrokerConfigurationService, useValue: service }] });
  return { ...result, service, directory, lane };
}

async function openConfirmation(): Promise<void> {
  fireEvent.click(await screen.findByRole('button', { name: OPEN }));
  await screen.findByRole('button', { name: CONFIRM });
}

describe('the switch to budgets', () => {
  it('asks first, says it cannot be undone, and submits only on the confirming click', async () => {
    const { service } = await setup();
    await screen.findByRole('button', { name: OPEN });
    expect(screen.getByText('Bots that will stop').nextElementSibling?.textContent).toBe('2');

    await openConfirmation();
    const heading = screen.getByRole('heading', { name: 'Switch to budgets now?' });
    await vi.waitFor(() => expect(document.activeElement).toBe(heading));
    expect(screen.getByText(/This cannot be undone\. The 2 running bots stop now\./)).toBeTruthy();
    expect(service.applyBudgetAuthority).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: CONFIRM }));
    const outcome = await screen.findByText('Budgets are on.');
    expect(service.applyBudgetAuthority).toHaveBeenCalledWith(expect.objectContaining({ accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4, idempotencyKey: expect.any(String) }), { review_token: 'review-old' });
    // The one-time card is gone; one line states the fact.
    expect(screen.queryByRole('heading', { name: 'Switch this account to budgets' })).toBeNull();
    expect(screen.queryByRole('button', { name: CONFIRM })).toBeNull();
    await vi.waitFor(() => expect(document.activeElement).toBe(outcome.closest('p')));
  });

  it('keeps the current setup when the owner backs out, and returns focus to the opener', async () => {
    const { service } = await setup();
    await openConfirmation();

    fireEvent.click(screen.getByRole('button', { name: 'Keep the current setup' }));

    const opener = await screen.findByRole('button', { name: OPEN });
    await vi.waitFor(() => expect(document.activeElement).toBe(opener));
    expect(service.applyBudgetAuthority).not.toHaveBeenCalled();
  });

  it('shows no card at all for an account already on budgets', async () => {
    await setup(BUDGET);

    expect(await screen.findByText('Budgets are on.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: OPEN })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Switch this account to budgets' })).toBeNull();
  });

  it('reads changed review evidence and requires another explicit click after a refusal', async () => {
    const { service } = await setup();
    service.applyBudgetAuthority.mockRejectedValueOnce(new HttpErrorResponse({ status: 409,
      error: { detail: { reason: 'budget_review_changed', message: 'The account changed. Review it again.', next_step: 'Review the refreshed active runs.' } } }));
    service.readBudgetAuthority.mockResolvedValue({ ...LEGACY, active_run_count: 3, review_token: 'review-next' });
    await openConfirmation();

    fireEvent.click(screen.getByRole('button', { name: CONFIRM }));
    const refusal = await screen.findByText('The account changed. Review it again.');
    await vi.waitFor(() => expect(document.activeElement).toBe(refusal.closest('[tabindex="-1"]')));
    await screen.findByText(/The 3 running bots stop now/);
    expect(service.applyBudgetAuthority).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: CONFIRM }));
    await screen.findByText('Budgets are on.');
    expect(service.applyBudgetAuthority.mock.calls[1][1]).toEqual({ review_token: 'review-next' });
  });

  it('recovers a lost response by reading state without repeating the switch', async () => {
    const { service } = await setup();
    service.applyBudgetAuthority.mockRejectedValueOnce(new HttpErrorResponse({ status: 0 }));
    service.readBudgetAuthority.mockResolvedValue(BUDGET);
    await openConfirmation();

    fireEvent.click(screen.getByRole('button', { name: CONFIRM }));
    await screen.findByText('Budgets are on.');
    expect(service.applyBudgetAuthority).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: CONFIRM })).toBeNull();
  });

  it('does not apply a result from the previous account to a new selection', async () => {
    const { fixture, service } = await setup();
    let resolve!: (value: typeof BUDGET) => void;
    service.applyBudgetAuthority.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    await openConfirmation();
    fireEvent.click(screen.getByRole('button', { name: CONFIRM }));
    service.readBudgetAuthority.mockResolvedValue({ ...LEGACY, account_id: 'PA10', review_token: 'other-account' });
    fixture.componentRef.setInput('accountId', 'PA10');
    await fixture.whenStable();
    resolve(BUDGET);
    await fixture.whenStable();
    expect(await screen.findByRole('button', { name: OPEN })).toBeTruthy();
    expect(screen.queryByText('Budgets are on.')).toBeNull();
  });

  it('has accessible review text and controls with the confirmation open', async () => {
    await setup();
    await openConfirmation();
    const result = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
    expect(result.violations).toEqual([]);
  });
});
