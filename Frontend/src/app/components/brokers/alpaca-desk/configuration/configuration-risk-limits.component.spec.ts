import { HttpErrorResponse } from '@angular/common/http';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { render, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import type { components } from '../../../../api/broker.types';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { AlpacaLiveVerdictService } from '../../../../services/alpaca-live-verdict.service';
import { BrokerConfigurationService } from './broker-configuration.service';
import { ConfigurationRiskLimitsComponent } from './configuration-risk-limits.component';

type State = components['schemas']['AccountRiskStateResponse'];
type LossHold = components['schemas']['AlpacaLiveVerdict']['loss_hold'];
const state: State = {
  account_id: 'PA-RISK', risk_revision: 3, selection_generation: 7,
  loss_fraction: .02, loss_usd: 100, applied_at_ms: 1788876060000,
  entry_state: 'ready', detail: 'These limits apply to new entries immediately.', limit_missing: false,
  hold_loss_limit_usd: null, hold_session_start_ms: null, hold_policy_revision: null,
};
const HELD: State = { ...state, entry_state: 'held', detail: 'A standing loss hold still blocks new entries.',
  hold_loss_limit_usd: 100, hold_session_start_ms: 1788840000000, hold_policy_revision: 2 };

const FRACTION = "Share of the day's starting equity (0 to 1)";
const CAP = 'At most (USD)';
const APPLY = 'Apply daily loss limit';
const REFUSAL = new HttpErrorResponse({ status: 409, error: {
  detail: { reason: 'revision_conflict', message: 'Risk limits changed since review.', next_step: 'Reload and review again.' },
} });

async function setup(initial: State = state, initialHold: LossHold | null = 'clear') {
  // The lane's loss-hold state as the shell's live verdict carries it; `null`
  // is a verdict that has not been read, or whose read failed.
  const lossHold = signal<LossHold | null>(initialHold);
  const service = {
    // A fresh object per call, like a real HTTP round-trip: reusing the same
    // reference would hide a re-seeding bug behind the resource's own
    // reference-equality check on its resolved value.
    readRiskLimits: vi.fn().mockImplementation(async () => ({ ...initial })),
    applyRiskLimits: vi.fn().mockResolvedValue({ ...initial, risk_revision: 4, loss_usd: 200 }),
    clearRiskHold: vi.fn().mockResolvedValue(state),
  };
  const view = await render(ConfigurationRiskLimitsComponent, {
    inputs: { clerkId: 'paper' }, providers: [
      { provide: BrokerConfigurationService, useValue: service },
      { provide: FleetDirectoryService, useValue: { lane: () => null } },
      { provide: AlpacaLiveVerdictService, useValue: {
        stateFor: () => ({ verdict: lossHold() === null ? null : { loss_hold: lossHold() }, lastError: null }),
      } },
    ],
  });
  await screen.findByRole('button', { name: APPLY });
  return { service, lossHold, view };
}

describe('ConfigurationRiskLimitsComponent (Daily loss limit)', () => {
  it('names both inputs, keeps edits inert until Apply, and lands focus on the outcome', async () => {
    const { service } = await setup();
    expect(screen.getByLabelText(FRACTION)).toBeTruthy();
    const cap = screen.getByLabelText(CAP);
    await userEvent.clear(cap);
    await userEvent.type(cap, '200');
    expect(service.applyRiskLimits).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: APPLY }));

    await waitFor(() => expect(service.applyRiskLimits).toHaveBeenCalledOnce());
    expect(service.applyRiskLimits.mock.calls[0][1]).toEqual({
      expected_risk_revision: 3, expected_selection_generation: 7,
      loss_fraction: .02, loss_usd: 200,
    });
    const outcome = await screen.findByText(/^Applied\. New entries use this limit now/);
    await waitFor(() => expect(document.activeElement).toBe(outcome));
    expect(screen.getByText(/at most \$200\.00/)).toBeTruthy();
  });

  it('does not retry a stale Apply, and moves focus to the server refusal', async () => {
    const { service } = await setup();
    service.applyRiskLimits.mockRejectedValue(REFUSAL);

    await userEvent.click(screen.getByRole('button', { name: APPLY }));

    const refusal = await screen.findByText('Risk limits changed since review.');
    await waitFor(() => expect(document.activeElement).toBe(refusal.closest('[tabindex="-1"]')));
    expect(service.applyRiskLimits).toHaveBeenCalledOnce();
    expect(screen.queryByText(/^Applied\./)).toBeNull();
  });

  it('keeps an edited draft after the refusal banner reloads the resource', async () => {
    // Reload firing after a stale-write refusal must not discard what the
    // operator already typed (linkedSignal does not freeze on its own —
    // see the sibling profile/account-evidence editors on this surface).
    const { service } = await setup();
    service.applyRiskLimits.mockRejectedValue(REFUSAL);

    const cap = screen.getByLabelText(CAP);
    await userEvent.clear(cap);
    await userEvent.type(cap, '250');
    await userEvent.click(screen.getByRole('button', { name: APPLY }));
    await screen.findByText('Risk limits changed since review.');

    await userEvent.click(screen.getByRole('button', { name: 'Reload configuration' }));
    await waitFor(() => expect(service.readRiskLimits).toHaveBeenCalledTimes(2));

    expect((screen.getByLabelText(CAP) as HTMLInputElement).value).toBe('250');
  });

  it('shows the hold beside the limit, clears it on request, and focuses the outcome', async () => {
    const { service } = await setup(HELD);
    expect(screen.getByText('On hold.')).toBeTruthy();
    expect(screen.getByText(/keeps the \$100\.00 limit it was raised\s+under/)).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: 'Clear hold' }));

    await waitFor(() => expect(service.clearRiskHold).toHaveBeenCalledOnce());
    expect(service.clearRiskHold.mock.calls[0][1]).toEqual({ expected_risk_revision: 3, expected_selection_generation: 7 });
    expect(service.applyRiskLimits).not.toHaveBeenCalled();
    const outcome = await screen.findByText(/^Hold cleared\./);
    await waitFor(() => expect(document.activeElement).toBe(outcome));
    expect(screen.getByText('No hold.')).toBeTruthy();
  });

  it('shows a hold raised while Settings is open, without a reload', async () => {
    const { service, lossHold } = await setup();
    expect(screen.getByText('No hold.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Clear hold' })).toBeNull();
    service.readRiskLimits.mockImplementation(async () => ({ ...HELD }));

    lossHold.set('held');

    expect(await screen.findByRole('button', { name: 'Clear hold' })).toBeTruthy();
    expect(screen.getByText('On hold.')).toBeTruthy();
    expect(service.readRiskLimits).toHaveBeenCalledTimes(2);
  });

  it('still shows a hold raised after a failed verdict read (clear, unread, held)', async () => {
    // A failed verdict read reports no hold state at all. It says nothing about
    // the hold, so the next known state is still compared with the last one.
    const { service, lossHold } = await setup();
    service.readRiskLimits.mockImplementation(async () => ({ ...HELD }));

    lossHold.set(null);
    TestBed.tick();
    lossHold.set('held');

    expect(await screen.findByRole('button', { name: 'Clear hold' })).toBeTruthy();
    expect(screen.getByText('On hold.')).toBeTruthy();
    expect(service.readRiskLimits).toHaveBeenCalledTimes(2);
  });

  it('keeps a hold raised while Apply is in flight, re-reads once it settles, and retires "Applied."', async () => {
    const { service, lossHold } = await setup();
    let settle: (value: State) => void = () => undefined;
    service.applyRiskLimits.mockImplementation(() => new Promise<State>((resolve) => { settle = resolve; }));
    await userEvent.click(screen.getByRole('button', { name: APPLY }));
    await waitFor(() => expect(service.applyRiskLimits).toHaveBeenCalledOnce());

    service.readRiskLimits.mockImplementation(async () => ({ ...HELD }));
    lossHold.set('held');
    TestBed.tick();
    expect(service.readRiskLimits).toHaveBeenCalledOnce();
    settle({ ...state, risk_revision: 4 });

    expect(await screen.findByRole('button', { name: 'Clear hold' })).toBeTruthy();
    expect(service.readRiskLimits).toHaveBeenCalledTimes(2);
    expect(screen.getByText('On hold.')).toBeTruthy();
    expect(screen.queryByText(/^Applied\./)).toBeNull();
  });

  it('keeps "Hold cleared." when the next verdict confirms the clear', async () => {
    const { service, lossHold } = await setup(HELD, 'held');
    await userEvent.click(screen.getByRole('button', { name: 'Clear hold' }));
    const outcome = await screen.findByText(/^Hold cleared\./);
    await waitFor(() => expect(document.activeElement).toBe(outcome));
    service.readRiskLimits.mockImplementation(async () => ({ ...state }));

    lossHold.set('clear');

    await waitFor(() => expect(service.readRiskLimits).toHaveBeenCalledTimes(2));
    expect(screen.getByText('No hold.')).toBeTruthy();
    expect(screen.getByText(/^Hold cleared\./)).toBe(outcome);
  });

  it('offers Check again when a limit is in force but the account is not judged yet', async () => {
    const { service } = await setup({ ...state, entry_state: 'unknown',
      detail: 'Alpaca has not confirmed this account’s cash and equity recently, so new entries wait.' });
    expect(screen.getByText('New entries wait.')).toBeTruthy();

    await userEvent.click(screen.getByRole('button', { name: 'Check again' }));

    await waitFor(() => expect(service.readRiskLimits).toHaveBeenCalledTimes(2));
  });

  it('names the missing limit as the cause, with the form as its fix, when none is set', async () => {
    await setup({ ...state, entry_state: 'unknown', risk_revision: 0, loss_fraction: null, loss_usd: null,
      applied_at_ms: null, limit_missing: true,
      detail: 'No daily loss limit is set for this account, so new entries are refused. Set one below and apply it.' });

    expect(screen.getByText(/Set one below and apply it\./)).toBeTruthy();
    expect(screen.getByText('No limit is in force yet.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Check again' })).toBeNull();
    expect(screen.queryByText(/Refresh account evidence/)).toBeNull();
  });

  it('takes whether a re-read can settle the state from the backend, not from the limit fields', async () => {
    // A simulated account keeps no limit of its own, yet what it waits on is
    // account evidence: the backend says the limit is not what is missing.
    const { service } = await setup({ ...state, entry_state: 'unknown', risk_revision: 0, loss_fraction: null,
      loss_usd: null, applied_at_ms: null, limit_missing: false,
      detail: 'Alpaca has not confirmed this account’s cash and equity recently, so new entries wait.' });

    await userEvent.click(screen.getByRole('button', { name: 'Check again' }));

    await waitFor(() => expect(service.readRiskLimits).toHaveBeenCalledTimes(2));
  });

  it('has no detectable accessibility violations while on hold', async () => {
    await setup(HELD);
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
