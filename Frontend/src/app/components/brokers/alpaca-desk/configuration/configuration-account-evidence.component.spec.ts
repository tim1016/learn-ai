import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerProfileRevision } from '../../../../api/alpaca.types';
import { ConfigurationAccountEvidenceComponent } from './configuration-account-evidence.component';

function revision(overrides: Partial<BrokerProfileRevision> = {}): BrokerProfileRevision {
  return {
    profile_id: 'profile-paper',
    revision: 2,
    schema_version: 1,
    credential_slot: 'default',
    endpoint_mode: 'paper',
    account_pin: null,
    account_pinned_at_ms: null,
    live_envelope: null,
    content_sha256: 'b'.repeat(64),
    complete: true,
    author_owner_id: 'owner-1',
    created_at_ms: 1_757_000_000_000,
    ...overrides,
  };
}

describe('ConfigurationAccountEvidenceComponent', () => {
  it('offers no way to type an account id', async () => {
    await render(ConfigurationAccountEvidenceComponent, {
      inputs: { revision: revision(), observed: null, nickname: null, busy: false },
    });

    // The only text box on this surface is the nickname, which is a label and
    // never the identity a pin is taken from.
    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(screen.getByText(/No account is approved/)).toBeTruthy();
  });

  it('approves only an account the broker was observed to reach', async () => {
    const pinRequested = vi.fn();
    await render(ConfigurationAccountEvidenceComponent, {
      inputs: {
        revision: revision(),
        observed: [{ account_id: 'PA3ZK9QWERTY', account_mode: 'paper', account_status: 'ACTIVE' }],
        nickname: null,
        busy: false,
      },
      on: { pinRequested },
    });

    await userEvent.click(screen.getByRole('button', { name: 'Approve this account' }));

    expect(pinRequested).toHaveBeenCalledWith('PA3ZK9QWERTY');
  });

  it('shows the declared endpoint and the observed mode as two separate facts', async () => {
    await render(ConfigurationAccountEvidenceComponent, {
      inputs: {
        revision: revision({ endpoint_mode: 'paper' }),
        observed: [{ account_id: '9LIVE0001', account_mode: 'live', account_status: 'ACTIVE' }],
        nickname: null,
        busy: false,
      },
    });

    expect(screen.getByText('Paper')).toBeTruthy();
    expect(screen.getByText('observed Live')).toBeTruthy();
  });

  it('renders an approved account id exactly, with its nickname as a separate label', async () => {
    await render(ConfigurationAccountEvidenceComponent, {
      inputs: {
        revision: revision({ account_pin: 'PA3ZK9QWERTY', account_pinned_at_ms: 1_757_000_000_000 }),
        observed: null,
        nickname: 'Testing account',
        busy: false,
      },
    });

    expect(screen.getByText('PA3ZK9QWERTY')).toBeTruthy();
    expect(screen.getByText('· Testing account')).toBeTruthy();
  });

  it('refuses a nickname before an account is approved', async () => {
    await render(ConfigurationAccountEvidenceComponent, {
      inputs: { revision: revision(), observed: null, nickname: null, busy: false },
    });

    expect(screen.queryByRole('button', { name: 'Save nickname' })).toBeNull();
  });

  it('says the broker reported nothing rather than rendering an empty list', async () => {
    await render(ConfigurationAccountEvidenceComponent, {
      inputs: { revision: revision(), observed: [], nickname: null, busy: false },
    });

    expect(screen.getByText('The broker reported no account for this revision.')).toBeTruthy();
  });
});
