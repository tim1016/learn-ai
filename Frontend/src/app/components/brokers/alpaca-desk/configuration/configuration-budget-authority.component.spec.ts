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

async function setup() {
  const lane = testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 3, routing_epoch: 4 });
  const directory = provideFleetDirectory({ observed_at_ms: 1, clerks: [lane] });
  const service = { readBudgetAuthority: vi.fn().mockResolvedValue(LEGACY), applyBudgetAuthority: vi.fn().mockResolvedValue(BUDGET) };
  const result = await render(ConfigurationBudgetAuthorityComponent, { inputs: { clerkId: 'clrk_spec', accountId: 'PA9' },
    providers: [directory, { provide: BrokerConfigurationService, useValue: service }] });
  await screen.findByRole('button', { name: 'Stop bots and use budgets' });
  return { ...result, service, directory, lane };
}

describe('budget authority switch', () => {
  it('shows the stopping consequence and submits the reviewed account and token only on click', async () => {
    const { service } = await setup();
    expect(screen.getByText(/2 active run\(s\) will be stopped/)).toBeTruthy();
    expect(screen.getByText(/Existing positions, orders and unsettled charges/)).toBeTruthy();
    expect(service.applyBudgetAuthority).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Stop bots and use budgets' }));
    await screen.findByText('Budget-backed Deploy is enabled for this account.');
    expect(service.applyBudgetAuthority).toHaveBeenCalledWith(expect.objectContaining({ accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4, idempotencyKey: expect.any(String) }), { review_token: 'review-old' });
    expect(screen.queryByRole('button', { name: 'Stop bots and use budgets' })).toBeNull();
  });

  it('reads changed review evidence and requires another explicit click after a refusal', async () => {
    const { service } = await setup();
    service.applyBudgetAuthority.mockRejectedValueOnce(new HttpErrorResponse({ status: 409,
      error: { detail: { reason: 'budget_review_changed', message: 'The account changed. Review it again.', next_step: 'Review the refreshed active runs.' } } }));
    service.readBudgetAuthority.mockResolvedValue({ ...LEGACY, active_run_count: 3, review_token: 'review-next' });
    fireEvent.click(screen.getByRole('button', { name: 'Stop bots and use budgets' }));
    await screen.findByText(/3 active run\(s\) will be stopped/);
    expect(service.applyBudgetAuthority).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Stop bots and use budgets' }));
    await screen.findByText('Budget-backed Deploy is enabled for this account.');
    expect(service.applyBudgetAuthority.mock.calls[1][1]).toEqual({ review_token: 'review-next' });
  });

  it('recovers a lost response by reading state without repeating the switch', async () => {
    const { service } = await setup();
    service.applyBudgetAuthority.mockRejectedValueOnce(new HttpErrorResponse({ status: 0 }));
    service.readBudgetAuthority.mockResolvedValue(BUDGET);
    fireEvent.click(screen.getByRole('button', { name: 'Stop bots and use budgets' }));
    await screen.findByText('Budget-backed Deploy is enabled for this account.');
    expect(service.applyBudgetAuthority).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Stop bots and use budgets' })).toBeNull();
  });

  it('does not apply a result from the previous account to a new selection', async () => {
    const { fixture, service } = await setup();
    let resolve!: (value: typeof BUDGET) => void;
    service.applyBudgetAuthority.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop bots and use budgets' }));
    service.readBudgetAuthority.mockResolvedValue({ ...LEGACY, account_id: 'PA10', review_token: 'other-account' });
    fixture.componentRef.setInput('accountId', 'PA10');
    await fixture.whenStable();
    resolve(BUDGET);
    await fixture.whenStable();
    expect(screen.getByText('PA10')).toBeTruthy();
    expect(screen.queryByText('Budget-backed Deploy is enabled for this account.')).toBeNull();
  });
  it('has accessible review text and controls', async () => {
    await setup();
    const result = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
    expect(result.violations).toEqual([]);
  });

});
