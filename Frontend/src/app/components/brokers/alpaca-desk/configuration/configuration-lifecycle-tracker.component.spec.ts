import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  AlpacaDeskState,
  BrokerInstallationSelection,
} from '../../../../api/alpaca.types';
import { ConfigurationLifecycleTrackerComponent } from './configuration-lifecycle-tracker.component';

function selection(
  overrides: Partial<BrokerInstallationSelection> = {},
): BrokerInstallationSelection {
  return {
    staged_profile_id: 'live-profile',
    staged_revision: 2,
    effective_profile_id: 'paper-profile',
    effective_revision: 3,
    effective_account_id: 'PA-1',
    effective_acknowledged_at_ms: 1_000,
    apply_requested: false,
    apply_requested_at_ms: null,
    last_apply_outcome: null,
    last_apply_refusal_reason: null,
    selection_generation: 7,
    ...overrides,
  };
}

function deskState(
  overrides: Partial<AlpacaDeskState> = {},
): AlpacaDeskState {
  return {
    activation_state: 'staged_not_applied',
    headline: 'A change is staged',
    detail: 'The staged revision is not effective yet.',
    consequence: 'Apply and restart to make it effective.',
    lifecycle: [
      { key: 'effective_configuration', label: 'Alpaca Paper is active', status: 'complete', status_label: 'Complete' },
      { key: 'selected_configuration', label: 'Alpaca Live is selected', status: 'current', status_label: 'Current step' },
      { key: 'worker_handoff', label: 'Restart worker', status: 'pending', status_label: 'Pending' },
    ],
    selection_label: 'Approved accounts',
    empty_choices_message: null,
    action: { kind: 'review_staged_configuration', label: 'Review', enabled: true },
    selection_generation: 7,
    staged_choice: null,
    effective_choice: null,
    choices: [],
    profiles_requiring_setup: 0,
    setup_required_message: null,
    ...overrides,
  };
}

describe('ConfigurationLifecycleTrackerComponent', () => {
  it('renders the backend lifecycle steps when generations agree', async () => {
    await render(ConfigurationLifecycleTrackerComponent, {
      componentInputs: {
        selection: selection(),
        deskState: deskState(),
      },
    });

    const tracker = screen.getByLabelText('Configuration change tracker');
    expect(tracker.textContent).toContain('Alpaca Paper is active');
    expect(tracker.textContent).toContain('Alpaca Live is selected');
    expect(tracker.textContent).toContain('Restart worker');
    expect(tracker.textContent).toContain('Current step');
  });

  it('renders the refreshing note and no lifecycle copy on a generation mismatch', async () => {
    await render(ConfigurationLifecycleTrackerComponent, {
      componentInputs: {
        selection: selection({ selection_generation: 8 }),
        deskState: deskState(),
      },
    });

    expect(screen.getByText('Refreshing / state unknown')).toBeTruthy();
    expect(screen.queryByText('Restart worker')).toBeNull();
  });

  it('treats an unread desk state as not yet matched rather than guessing', async () => {
    await render(ConfigurationLifecycleTrackerComponent, {
      componentInputs: { selection: selection(), deskState: null },
    });

    expect(screen.getByText('Refreshing / state unknown')).toBeTruthy();
  });

  it('says Apply is recorded when the adopted selection says so', async () => {
    await render(ConfigurationLifecycleTrackerComponent, {
      componentInputs: {
        selection: selection({
          apply_requested: true,
          apply_requested_at_ms: 2_000,
        }),
        deskState: deskState(),
      },
    });

    expect(screen.getByText(/Apply is recorded/)).toBeTruthy();
    expect(screen.getByText(/never from this browser/)).toBeTruthy();
  });

  it('never offers a restart control', async () => {
    await render(ConfigurationLifecycleTrackerComponent, {
      componentInputs: { selection: selection(), deskState: deskState() },
    });

    expect(screen.queryByRole('button')).toBeNull();
  });
});
