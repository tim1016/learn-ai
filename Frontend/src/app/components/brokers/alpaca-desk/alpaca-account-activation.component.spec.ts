import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { AlpacaDeskState } from '../../../api/alpaca.types';
import { AlpacaAccountActivationComponent } from './alpaca-account-activation.component';

const deskState: AlpacaDeskState = {
  activation_state: 'no_selection',
  headline: 'No Alpaca account is active',
  detail: 'Choose an approved account to prepare this desk.',
  lifecycle: [
    {
      key: 'effective_configuration',
      label: 'No active account',
      status: 'current',
      status_label: 'Current step',
    },
    {
      key: 'selected_configuration',
      label: 'Select configuration',
      status: 'pending',
      status_label: 'Pending',
    },
    {
      key: 'worker_handoff',
      label: 'Restart worker',
      status: 'pending',
      status_label: 'Pending',
    },
  ],
  selection_label: 'Approved accounts',
  empty_choices_message: 'No approved accounts are available. Verify one in Configuration.',
  consequence: 'Applying changes startup configuration and requires a controlled restart.',
  action: {
    kind: 'review_configuration',
    label: 'Review & apply selected account',
    enabled: true,
  },
  selection_generation: 7,
  staged_choice: null,
  effective_choice: null,
  choices: [
    {
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
      is_effective: false,
      action_kind: 'review_configuration',
      action_label: 'Select Paper account',
    },
    {
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      account_id: 'LIVE-456',
      nickname: null,
      account_label: 'LIVE-456',
      endpoint_mode: 'live',
      badge_label: 'Live',
      description: 'Uses the approved live brokerage account.',
      is_staged: false,
      is_effective: false,
      action_kind: 'review_configuration',
      action_label: 'Select Live account',
    },
  ],
  profiles_requiring_setup: 0,
  setup_required_message: null,
};

describe('AlpacaAccountActivationComponent', () => {
  it('renders backend-authored guidance and keeps review disabled until an account is chosen', async () => {
    await render(AlpacaAccountActivationComponent, { inputs: { view: deskState } });

    expect(screen.getByRole('heading', { name: deskState.headline })).toBeTruthy();
    expect(screen.getByText(deskState.detail)).toBeTruthy();
    expect(screen.getByText(deskState.consequence)).toBeTruthy();
    expect(screen.getByRole('group', { name: deskState.selection_label })).toBeTruthy();
    expect(screen.getByText('Current step')).toBeTruthy();
    expect(screen.getAllByText('Pending')).toHaveLength(2);
    expect(screen.getByRole('button', { name: deskState.action.label })).toHaveProperty(
      'disabled',
      true,
    );
  });

  it('renders approved identities exactly and enables review for the selected account', async () => {
    await render(AlpacaAccountActivationComponent, { inputs: { view: deskState } });

    await userEvent.click(screen.getByRole('radio', { name: /Strategy lab · PA-123/ }));

    expect(screen.getByText('PA-123')).toBeTruthy();
    expect(screen.getByText('Revision 3')).toBeTruthy();
    expect(screen.getByText('LIVE-456')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Select Paper account' })).toHaveProperty(
      'disabled',
      false,
    );
  });

  it('emits the chosen account for configuration review', async () => {
    const reviewRequested = vi.fn();
    await render(AlpacaAccountActivationComponent, {
      inputs: { view: deskState },
      on: { reviewRequested },
    });

    await userEvent.click(screen.getByRole('radio', { name: /Strategy lab · PA-123/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Select Paper account' }));

    expect(reviewRequested).toHaveBeenCalledWith(deskState.choices[0]);
  });

  it('preselects the backend-staged choice for review without staging again', async () => {
    const stagedState: AlpacaDeskState = {
      ...deskState,
      staged_choice: deskState.choices[0],
      action: {
        kind: 'review_staged_configuration',
        label: 'Review staged account',
        enabled: true,
      },
    };

    await render(AlpacaAccountActivationComponent, { inputs: { view: stagedState } });

    expect(screen.getByRole('radio', { name: /Strategy lab · PA-123/ })).toHaveProperty(
      'checked',
      true,
    );
    expect(screen.getByRole('button', { name: 'Review staged account' })).toHaveProperty(
      'disabled',
      false,
    );
  });

  it('keeps the backend-authored setup action available when no approved account exists', async () => {
    const reviewRequested = vi.fn();
    const emptyState = {
      ...deskState,
      choices: [],
      action: { kind: 'review_configuration' as const, label: 'Set up an account', enabled: true },
    };

    await render(AlpacaAccountActivationComponent, {
      inputs: { view: emptyState },
      on: { reviewRequested },
    });

    expect(
      screen.getByText('No approved accounts are available. Verify one in Configuration.'),
    ).toBeTruthy();
    const setup = screen.getByRole('button', { name: 'Set up an account' });
    expect(setup).toHaveProperty('disabled', false);
    await userEvent.click(setup);
    expect(reviewRequested).toHaveBeenCalledWith(null);
  });

  it('renders backend guidance for profiles that still need verification', async () => {
    const setupMessage = '1 saved profile still needs account verification before it can be selected.';
    await render(AlpacaAccountActivationComponent, {
      inputs: {
        view: { ...deskState, profiles_requiring_setup: 1, setup_required_message: setupMessage },
      },
    });

    expect(screen.getByText(setupMessage)).toBeTruthy();
  });

  it('reviews an exact staged revision even when it is no longer offered as a latest choice', async () => {
    const reviewRequested = vi.fn();
    const staged = deskState.choices[0];
    const stagedState: AlpacaDeskState = {
      ...deskState,
      activation_state: 'apply_requested_restart_required',
      staged_choice: staged,
      choices: [],
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    };
    await render(AlpacaAccountActivationComponent, {
      inputs: { view: stagedState },
      on: { reviewRequested },
    });

    await userEvent.click(screen.getByRole('button', { name: 'View restart steps' }));

    expect(reviewRequested).toHaveBeenCalledWith(staged);
  });

  it('keeps the restart action after Apply is recorded for the preselected staged choice', async () => {
    const staged = deskState.choices[0];
    const restartState: AlpacaDeskState = {
      ...deskState,
      activation_state: 'apply_requested_restart_required',
      staged_choice: staged,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    };
    await render(AlpacaAccountActivationComponent, { inputs: { view: restartState } });

    expect(screen.getByRole('radio', { name: /Strategy lab · PA-123/ })).toHaveProperty(
      'checked',
      true,
    );
    expect(screen.getByRole('button', { name: 'View restart steps' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Select Paper account' })).toBeNull();
  });
});
