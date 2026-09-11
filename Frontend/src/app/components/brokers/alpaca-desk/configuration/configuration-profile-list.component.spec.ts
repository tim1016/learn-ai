import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerInstallationSelection, BrokerProfile } from '../../../../api/alpaca.types';
import { ConfigurationProfileListComponent } from './configuration-profile-list.component';

function profile(overrides: Partial<BrokerProfile> = {}): BrokerProfile {
  return {
    profile_id: 'profile-paper',
    owner_id: 'owner-1',
    broker: 'alpaca',
    display_name: 'Paper — strategy testing',
    archived: false,
    created_at_ms: 1_757_000_000_000,
    updated_at_ms: 1_757_000_000_000,
    ...overrides,
  };
}

function selection(
  overrides: Partial<BrokerInstallationSelection> = {},
): BrokerInstallationSelection {
  return {
    staged_profile_id: null,
    staged_revision: null,
    apply_requested: false,
    apply_requested_at_ms: null,
    apply_requested_generation: null,
    selection_generation: 1,
    effective_profile_id: null,
    effective_revision: null,
    effective_account_id: null,
    effective_acknowledged_at_ms: null,
    last_apply_outcome: null,
    last_apply_refusal_reason: null,
    ...overrides,
  };
}

describe('ConfigurationProfileListComponent', () => {
  it('says the list is empty rather than rendering an empty frame', async () => {
    await render(ConfigurationProfileListComponent, {
      inputs: { profiles: [], selection: selection(), selectedProfileId: null, busy: false },
    });

    expect(screen.getByText(/No configuration profiles are saved yet/)).toBeTruthy();
  });

  it('marks which profile is effective and which is merely staged', async () => {
    await render(ConfigurationProfileListComponent, {
      inputs: {
        profiles: [profile(), profile({ profile_id: 'profile-live', display_name: 'Live' })],
        selection: selection({
          effective_profile_id: 'profile-paper',
          staged_profile_id: 'profile-live',
        }),
        selectedProfileId: null,
        busy: false,
      },
    });

    expect(screen.getByText('Effective')).toBeTruthy();
    expect(screen.getByText('Staged')).toBeTruthy();
  });

  it('refuses to stage an archived profile and offers to restore it instead', async () => {
    const archiveToggled = vi.fn();
    await render(ConfigurationProfileListComponent, {
      inputs: {
        profiles: [profile({ archived: true })],
        selection: selection(),
        selectedProfileId: null,
        busy: false,
      },
      on: { archiveToggled },
    });

    expect(screen.getByRole('button', { name: 'Stage' }).hasAttribute('disabled')).toBe(true);
    await userEvent.click(screen.getByRole('button', { name: 'Restore' }));

    expect(archiveToggled).toHaveBeenCalledWith({ profileId: 'profile-paper', archived: false });
  });

  it('disables every write while another one is in flight', async () => {
    await render(ConfigurationProfileListComponent, {
      inputs: {
        profiles: [profile()],
        selection: selection(),
        selectedProfileId: null,
        busy: true,
      },
    });

    expect(screen.getByRole('button', { name: 'Stage' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: 'Archive' }).hasAttribute('disabled')).toBe(true);
  });
});
