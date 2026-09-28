import { HttpErrorResponse } from '@angular/common/http';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { components } from '../../../../api/broker.types';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { BrokerConfigurationService } from './broker-configuration.service';
import { ConfigurationRiskLimitsComponent } from './configuration-risk-limits.component';

type State = components['schemas']['AccountRiskStateResponse'];
const state: State = {
  account_id: 'PA-RISK', risk_revision: 3, selection_generation: 7,
  loss_fraction: .02, loss_usd: 100, applied_at_ms: 1788876060000,
  entry_state: 'ready', detail: 'These limits apply to new entries immediately.',
  hold_loss_limit_usd: null, hold_session_start_ms: null, hold_policy_revision: null,
};

async function setup(initial = state) {
  const service = {
    readRiskLimits: vi.fn().mockResolvedValue(initial),
    applyRiskLimits: vi.fn().mockResolvedValue({ ...initial, risk_revision: 4, loss_usd: 200 }),
    clearRiskHold: vi.fn().mockResolvedValue(state),
  };
  await render(ConfigurationRiskLimitsComponent, {
    inputs: { clerkId: 'paper' }, providers: [
      { provide: BrokerConfigurationService, useValue: service },
      { provide: FleetDirectoryService, useValue: { lane: () => null } },
    ],
  });
  await screen.findByRole('button', { name: 'Apply risk limits' });
  return service;
}

describe('ConfigurationRiskLimitsComponent', () => {
  it('keeps edits inert until Apply and submits the exact reviewed revision', async () => {
    const service = await setup();
    const cap = screen.getByRole('spinbutton', { name: 'Loss cap (USD)' });
    await userEvent.clear(cap);
    await userEvent.type(cap, '200');
    expect(service.applyRiskLimits).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Apply risk limits' }));
    await waitFor(() => expect(service.applyRiskLimits).toHaveBeenCalledOnce());
    expect(service.applyRiskLimits.mock.calls[0][1]).toEqual({
      expected_risk_revision: 3, expected_selection_generation: 7,
      loss_fraction: .02, loss_usd: 200,
    });
    expect(await screen.findByText('Risk limits applied. No restart or redeployment is needed.')).toBeTruthy();
    expect(screen.getByText('Account PA-RISK · Revision 4')).toBeTruthy();
  });

  it('does not retry a stale Apply and displays the server refusal', async () => {
    const service = await setup();
    service.applyRiskLimits.mockRejectedValue(new HttpErrorResponse({ status: 409, error: {
      detail: { reason: 'revision_conflict', message: 'Risk limits changed since review.', next_step: 'Reload and review again.' },
    } }));
    await userEvent.click(screen.getByRole('button', { name: 'Apply risk limits' }));
    expect(await screen.findByText('Risk limits changed since review.')).toBeTruthy();
    expect(service.applyRiskLimits).toHaveBeenCalledOnce();
    expect(screen.queryByText('Risk limits applied. No restart or redeployment is needed.')).toBeNull();
  });

  it('exposes the guarded Clear action and never treats a limit edit as clearing', async () => {
    const service = await setup({ ...state, entry_state: 'held', hold_loss_limit_usd: 100,
      hold_session_start_ms: 1788840000000, hold_policy_revision: 2 });
    expect(screen.getByText(/Standing hold retains its original/)).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Clear loss hold' }));
    await waitFor(() => expect(service.clearRiskHold).toHaveBeenCalledOnce());
    expect(service.clearRiskHold.mock.calls[0][1]).toEqual({ expected_risk_revision: 3, expected_selection_generation: 7 });
    expect(service.applyRiskLimits).not.toHaveBeenCalled();
  });
});
