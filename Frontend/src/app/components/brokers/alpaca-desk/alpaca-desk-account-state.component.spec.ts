import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  AlpacaDeskAccountChoice,
  AlpacaDeskState,
  BrokerAccountSnapshot,
} from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountStateComponent } from './alpaca-desk-account-state.component';

function snapshot(
  overrides: Partial<BrokerAccountSnapshot> = {},
): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA-123',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 1_000,
    equity: 1_000,
    buying_power: 2_000,
    portfolio_value: 1_000,
    long_market_value: 0,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: null,
    observed_at_ms: 1,
    ...overrides,
  };
}

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
    action_consequence: 'Review this saved revision without changing the running worker.',
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

  it('shows a pending first selection when connectivity succeeds before an effective selection exists', async () => {
    const staged = choice({
      is_staged: true,
      is_effective: false,
      action_kind: 'view_restart_steps',
      action_label: 'View restart steps',
    });
    const current = state({
      activation_state: 'apply_requested_restart_required',
      headline: 'Strategy lab is ready for a controlled restart',
      detail: 'Apply is recorded for this installation.',
      consequence: 'The running worker has not changed.',
      effective_choice: null,
      staged_choice: staged,
      choices: [staged],
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

  it('shows the effective identity from effective_choice, never from staged state', async () => {
    const effective = choice();
    const staged = choice({
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
    });
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: effective,
      staged_choice: staged,
      action: { kind: 'review_staged_configuration', label: 'Review & apply Alpaca Live', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot(),
      },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    const strip = screen.getByLabelText('Effective broker identity');
    expect(within(strip).getByText(effective.profile_label)).toBeTruthy();
    expect(within(strip).getByText('Revision 3')).toBeTruthy();
    expect(within(strip).getByText(effective.account_label)).toBeTruthy();
    expect(within(strip).getByText('Paper')).toBeTruthy();
    expect(within(strip).queryByText('Alpaca Live')).toBeNull();
    expect(within(strip).queryByText('Live')).toBeNull();
  });

  it('falls back to the observed snapshot without fabricating a revision', async () => {
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: null,
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot({ account_mode: 'live', account_id: 'LIVE-9' }),
      },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    const strip = screen.getByLabelText('Effective broker identity');
    expect(within(strip).getByText('LIVE-9')).toBeTruthy();
    expect(within(strip).getByText('Live')).toBeTruthy();
    expect(within(strip).queryByText(/^Revision/)).toBeNull();
    expect(within(strip).getByText(/no revision yet/)).toBeTruthy();
  });

  it('warns and gates identity-dependent actions when account ids disagree', async () => {
    const reviewRequested = vi.fn();
    const effective = choice({ account_id: 'PA-DECLARED' });
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: effective,
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot({ account_id: 'PA-OBSERVED' }),
      },
      on: { reviewRequested },
      providers: [{ provide: BrokersService, useValue: brokersService() }],
    });

    const warning = screen.getByRole('alert');
    expect(warning.textContent).toContain('PA-DECLARED');
    expect(warning.textContent).toContain('PA-OBSERVED');

    const action = screen.getByRole('button', { name: current.action.label });
    expect(action).toHaveProperty('disabled', true);
    await fireEvent.click(action);
    expect(reviewRequested).not.toHaveBeenCalled();
  });
});
