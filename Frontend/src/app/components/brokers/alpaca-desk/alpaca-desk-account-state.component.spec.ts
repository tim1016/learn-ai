import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { AlpacaDeskAccountChoice, AlpacaDeskState } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountStateComponent } from './alpaca-desk-account-state.component';

function choice(
  overrides: Partial<AlpacaDeskAccountChoice> = {},
): AlpacaDeskAccountChoice {
  return {
    selection_id: 'paper-profile:3',
    profile_id: 'paper-profile',
    revision: 3,
    profile_label: 'Alpaca Paper',
    account_id: 'PA-123',
    nickname: 'Strategy lab',
    account_label: 'Strategy lab · PA-123',
    endpoint_mode: 'paper',
    badge_label: 'Paper',
    description: 'For strategy testing with no live capital.',
    is_staged: false,
    is_effective: true,
    action_kind: 'review_configuration',
    action_label: 'Review Alpaca Paper',
    ...overrides,
  };
}

function state(overrides: Partial<AlpacaDeskState> = {}): AlpacaDeskState {
  const effective = choice();
  return {
    activation_state: 'effective_selection',
    headline: 'Strategy lab is the effective selection',
    detail: 'The worker last acknowledged Alpaca Paper.',
    lifecycle: [
      {
        key: 'effective_configuration',
        label: 'Alpaca Paper is active',
        status: 'complete',
        status_label: 'Complete',
      },
    ],
    selection_label: 'Approved accounts',
    empty_choices_message: null,
    consequence: 'The account could not be reached; no trading controls are available.',
    action: {
      kind: 'review_configuration',
      label: 'Review account configuration',
      enabled: true,
    },
    selection_generation: 9,
    staged_choice: effective,
    effective_choice: effective,
    choices: [effective],
    profiles_requiring_setup: 0,
    setup_required_message: null,
    ...overrides,
  };
}

function brokersService(): Pick<BrokersService, 'getAccount'> {
  return {
    getAccount: vi.fn(() => new Promise<never>(() => undefined)),
  };
}

describe('AlpacaDeskAccountStateComponent', () => {
  it('routes a failed connection to the staged revision named by the action', async () => {
    const effective = choice();
    const staged = choice({
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      account_id: 'LIVE-456',
      account_label: 'LIVE-456',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
      action_label: 'Review Alpaca Live',
    });
    const reviewRequested = vi.fn();
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: effective,
      staged_choice: staged,
      action: {
        kind: 'review_staged_configuration',
        label: 'Review & apply Alpaca Live',
        enabled: true,
      },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: false, accountFailed: true },
      on: { reviewRequested },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    await fireEvent.click(screen.getByRole('button', { name: current.action.label }));

    expect(reviewRequested).toHaveBeenCalledWith(staged);
    expect(reviewRequested).not.toHaveBeenCalledWith(effective);
  });

  it('separates the effective account from a pending restart when connectivity fails', async () => {
    const effective = choice();
    const staged = choice({
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      account_id: 'LIVE-456',
      account_label: 'LIVE-456',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
    });
    const current = state({
      activation_state: 'apply_requested_restart_required',
      headline: 'Alpaca Live is ready for the next controlled restart',
      detail: 'The current worker still owns Alpaca Paper.',
      consequence: 'Restart this installation before Alpaca Live can become effective.',
      effective_choice: effective,
      staged_choice: staged,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: false, accountFailed: true },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    const effectiveSection = screen.getByText('Effective configuration').closest('section');
    if (effectiveSection === null) throw new Error('effective configuration section missing');
    expect(within(effectiveSection).getByText(effective.profile_label)).toBeTruthy();
    expect(within(effectiveSection).getByText(/Strategy lab · PA-123.*Paper.*Revision 3/)).toBeTruthy();
    expect(within(effectiveSection).queryByText(current.headline)).toBeNull();

    const pendingSection = screen.getByText('Configuration change pending').closest('section');
    if (pendingSection === null) throw new Error('pending configuration section missing');
    expect(within(pendingSection).getByText(current.headline)).toBeTruthy();
    expect(within(pendingSection).getByText(current.consequence)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: current.action.label })).toHaveLength(1);
  });

  it('keeps a connected account visible with the full backend-authored pending warning', async () => {
    const staged = choice({
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
    });
    const current = state({
      activation_state: 'apply_requested_restart_required',
      headline: 'Alpaca Live is selected, but not effective for this installation',
      detail: 'The current worker still owns Alpaca Paper.',
      consequence: 'Restart this installation before Alpaca Live can become effective.',
      staged_choice: staged,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: true, accountFailed: false },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    expect(screen.getByText(current.headline)).toBeTruthy();
    expect(screen.getByText(current.detail)).toBeTruthy();
    expect(screen.getByText(current.consequence)).toBeTruthy();
    expect(screen.getByRole('button', { name: current.action.label })).toBeTruthy();
  });

  it('honors a backend-disabled pending action and emits nothing', async () => {
    const reviewRequested = vi.fn();
    const current = state({
      activation_state: 'staged_not_applied',
      headline: 'A configuration change is pending',
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: false },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: true, accountFailed: false },
      on: { reviewRequested },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    const action = screen.getByRole('button', { name: current.action.label });
    expect(action).toHaveProperty('disabled', true);
    await fireEvent.click(action);
    expect(reviewRequested).not.toHaveBeenCalled();
  });
});
