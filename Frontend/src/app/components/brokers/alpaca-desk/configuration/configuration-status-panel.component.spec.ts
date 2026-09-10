import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  BrokerAccountNickname,
  BrokerInstallationSelection,
  BrokerProfile,
} from '../../../../api/alpaca.types';
import { ConfigurationStatusPanelComponent } from './configuration-status-panel.component';

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
    selection_generation: 3,
    effective_profile_id: null,
    effective_revision: null,
    effective_account_id: null,
    effective_acknowledged_at_ms: null,
    last_apply_outcome: null,
    last_apply_refusal_reason: null,
    ...overrides,
  };
}

async function renderPanel(
  current: BrokerInstallationSelection,
  nicknames: readonly BrokerAccountNickname[] = [],
) {
  return render(ConfigurationStatusPanelComponent, {
    inputs: { selection: current, profiles: [profile()], nicknames, busy: false },
  });
}

describe('ConfigurationStatusPanelComponent', () => {
  it('says no broker is bound when nothing has been applied', async () => {
    await renderPanel(selection());

    expect(screen.getByText(/No revision has been applied/)).toBeTruthy();
    expect(screen.getByText('Nothing is staged.')).toBeTruthy();
  });

  it('keeps staged and effective distinct and asks for a controlled restart between them', async () => {
    await renderPanel(
      selection({
        staged_profile_id: 'profile-paper',
        staged_revision: 4,
        effective_profile_id: 'profile-paper',
        effective_revision: 3,
      }),
    );

    expect(screen.getByText('Paper — strategy testing · revision 4')).toBeTruthy();
    expect(screen.getByText('Paper — strategy testing · revision 3')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toContain('controlled restart');
  });

  it('reports an applied request as not yet effective, and refuses a second apply', async () => {
    await renderPanel(
      selection({
        staged_profile_id: 'profile-paper',
        staged_revision: 4,
        apply_requested: true,
        apply_requested_at_ms: 1_757_000_500_000,
      }),
    );

    const status = screen.getAllByRole('status')[0];
    expect(status.textContent).toContain('nothing has changed yet');
    expect(screen.getByRole('button', { name: 'Apply staged revision' }).hasAttribute('disabled'))
      .toBe(true);
  });

  it('never suggests Apply arms live trading', async () => {
    await renderPanel(selection({ staged_profile_id: 'profile-paper', staged_revision: 1 }));

    expect(screen.getByText(/Apply never arms live trading/)).toBeTruthy();
  });

  it('renders the effective account id exactly and its nickname beside it', async () => {
    await renderPanel(
      selection({
        effective_profile_id: 'profile-paper',
        effective_revision: 2,
        effective_account_id: 'PA3ZK9QWERTY',
        effective_acknowledged_at_ms: 1_757_000_000_000,
      }),
      [{ account_id: 'PA3ZK9QWERTY', nickname: 'Testing account', updated_at_ms: 1 }],
    );

    expect(screen.getByText('PA3ZK9QWERTY')).toBeTruthy();
    expect(screen.getByText('· Testing account')).toBeTruthy();
    expect(screen.getByText(/records a past boot/)).toBeTruthy();
  });

  it('renders a refusal reason as the backend wrote it, and the outcome as a receipt label', async () => {
    await renderPanel(
      selection({
        last_apply_outcome: 'refused',
        last_apply_refusal_reason:
          'applying profile-paper@4 would leave account PA3ZK9QWERTY with 2 open orders',
      }),
    );

    expect(screen.getByText('Last apply · Refused')).toBeTruthy();
    expect(
      screen.getByText(
        'applying profile-paper@4 would leave account PA3ZK9QWERTY with 2 open orders',
      ),
    ).toBeTruthy();
  });

  it('names an unexplained profile id byte-for-byte rather than rewriting it', async () => {
    await renderPanel(
      selection({ effective_profile_id: 'profile_archived_01', effective_revision: 9 }),
    );

    expect(screen.getByText('profile_archived_01 · revision 9')).toBeTruthy();
  });
});
