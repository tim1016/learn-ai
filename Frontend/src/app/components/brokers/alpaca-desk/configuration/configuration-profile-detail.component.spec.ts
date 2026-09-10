import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type {
  BrokerCredentialSlot,
  BrokerProfileDetail,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';
import { ConfigurationProfileDetailComponent } from './configuration-profile-detail.component';

const SLOTS: readonly BrokerCredentialSlot[] = [
  { slot: 'alpaca_paper_primary', label: 'Paper — primary', available: true },
];

function revision(overrides: Partial<BrokerProfileRevision> = {}): BrokerProfileRevision {
  return {
    profile_id: 'profile-paper',
    revision: 3,
    schema_version: 1,
    credential_slot: 'alpaca_paper_primary',
    endpoint_mode: 'paper',
    account_pin: null,
    account_pinned_at_ms: null,
    live_envelope: null,
    content_sha256: 'd'.repeat(64),
    complete: true,
    author_owner_id: 'owner-1',
    created_at_ms: 1_757_000_000_000,
    ...overrides,
  };
}

function detail(overrides: Partial<BrokerProfileDetail> = {}): BrokerProfileDetail {
  return {
    profile: {
      profile_id: 'profile-paper',
      owner_id: 'owner-1',
      broker: 'alpaca',
      display_name: 'Paper — strategy testing',
      archived: false,
      created_at_ms: 1_757_000_000_000,
      updated_at_ms: 1_757_000_000_000,
    },
    latest_revision: revision(),
    ...overrides,
  };
}

async function renderDetail(overrides: Partial<BrokerProfileDetail> = {}, on = {}) {
  return render(ConfigurationProfileDetailComponent, {
    inputs: {
      detail: detail(overrides),
      revisions: [revision()],
      slots: SLOTS,
      observed: null,
      nickname: null,
      busy: false,
    },
    on,
  });
}

describe('ConfigurationProfileDetailComponent', () => {
  it('writes the next revision against the one the editor was opened on', async () => {
    const revisionSaved = vi.fn();
    await renderDetail({}, { revisionSaved });

    await userEvent.click(screen.getByRole('button', { name: 'Save revision 4' }));

    expect(revisionSaved).toHaveBeenCalledWith({
      expectedRevision: 3,
      content: {
        credential_slot: 'alpaca_paper_primary',
        endpoint_mode: 'paper',
        live_envelope: null,
      },
    });
  });

  it('keeps a clone name that was refused, so it does not have to be retyped', async () => {
    const cloned = vi.fn();
    await renderDetail({}, { cloned });
    const field = screen.getByLabelText('Clone as');
    await userEvent.type(field, 'Paper — overnight');

    await userEvent.click(screen.getByRole('button', { name: 'Clone' }));

    expect(cloned).toHaveBeenCalledWith('Paper — overnight');
    // A refusal leaves the same profile open; the typed name must survive it.
    expect((field as HTMLInputElement).value).toBe('Paper — overnight');
  });

  it('names the slot the directory explains, and keeps the content hash exact', async () => {
    const { container } = await renderDetail();
    const facts = container.querySelector('.detail__facts');

    expect(facts).not.toBeNull();
    expect(within(facts as HTMLElement).getByText('Paper — primary')).toBeTruthy();
    expect(screen.getByText('d'.repeat(64))).toBeTruthy();
  });

  it('shows a slot the directory no longer lists as its stored key', async () => {
    await renderDetail({ latest_revision: revision({ credential_slot: 'retired_slot' }) });

    expect(screen.getByText('Retired Slot')).toBeTruthy();
  });

  it('says a profile with no revision has none, rather than rendering an empty editor state', async () => {
    await renderDetail({ latest_revision: null });

    expect(screen.getByText('This profile has no revision yet.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Save revision 1' })).toBeTruthy();
  });
});
