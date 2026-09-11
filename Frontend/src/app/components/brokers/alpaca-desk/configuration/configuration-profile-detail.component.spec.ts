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

    await userEvent.click(screen.getByRole('button', { name: 'Save next revision' }));

    expect(revisionSaved).toHaveBeenCalledWith({
      expectedRevision: 3,
      content: {
        credential_slot: 'alpaca_paper_primary',
        endpoint_mode: 'paper',
        live_envelope: null,
      },
    });
  });

  it('keeps a half-typed revision across a write that did not touch the revision', async () => {
    // A rename or an account approval reloads the profile detail, handing this
    // component a new object for the *same* revision. The editor must not treat
    // that as a new revision to re-seed from: an operator part-way through
    // typing a live envelope would silently lose every value they had entered.
    const rendered = await renderDetail();
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');
    await userEvent.type(screen.getByRole('spinbutton', { name: 'Daily loss cap (USD)' }), '5000');

    rendered.fixture.componentRef.setInput('detail', detail({ latest_revision: revision() }));
    await rendered.fixture.whenStable();

    expect((screen.getByLabelText('Endpoint') as HTMLSelectElement).value).toBe('live');
    expect(
      (screen.getByRole('spinbutton', { name: 'Daily loss cap (USD)' }) as HTMLInputElement).value,
    ).toBe('5000');
  });

  it('re-seeds the editor when a genuinely newer revision arrives', async () => {
    const rendered = await renderDetail();
    await userEvent.selectOptions(screen.getByLabelText('Endpoint'), 'live');

    rendered.fixture.componentRef.setInput(
      'detail',
      detail({ latest_revision: revision({ revision: 4 }) }),
    );
    await rendered.fixture.whenStable();

    expect((screen.getByLabelText('Endpoint') as HTMLSelectElement).value).toBe('paper');
    expect(screen.getByRole('button', { name: 'Save next revision' })).toBeTruthy();
  });

  it('keeps a clone name that was refused, so it does not have to be retyped', async () => {
    const cloned = vi.fn();
    const rendered = await renderDetail({}, { cloned });
    const field = screen.getByLabelText('Clone as');
    await userEvent.type(field, 'Paper — overnight');

    await userEvent.click(screen.getByRole('button', { name: 'Clone' }));
    // A refusal leaves the same profile open, and the page still reloads the
    // detail — a fresh object for the same profile must not clear the field.
    rendered.fixture.componentRef.setInput('detail', detail());
    await rendered.fixture.whenStable();

    expect(cloned).toHaveBeenCalledWith('Paper — overnight');
    expect((screen.getByLabelText('Clone as') as HTMLInputElement).value).toBe('Paper — overnight');
  });

  it('clears the clone name once a different profile is open', async () => {
    const rendered = await renderDetail();
    await userEvent.type(screen.getByLabelText('Clone as'), 'Paper — overnight');

    rendered.fixture.componentRef.setInput(
      'detail',
      detail({ profile: { ...detail().profile, profile_id: 'profile-clone' } }),
    );
    await rendered.fixture.whenStable();

    expect((screen.getByLabelText('Clone as') as HTMLInputElement).value).toBe('');
  });

  it('keeps a half-typed rename across a write that did not rename the profile', async () => {
    const rendered = await renderDetail();
    const field = screen.getByLabelText('Profile name');
    await userEvent.clear(field);
    await userEvent.type(field, 'Paper — overnight');

    rendered.fixture.componentRef.setInput('detail', detail());
    await rendered.fixture.whenStable();

    expect((screen.getByLabelText('Profile name') as HTMLInputElement).value).toBe(
      'Paper — overnight',
    );
  });

  it('adopts a name that was changed elsewhere', async () => {
    const rendered = await renderDetail();

    rendered.fixture.componentRef.setInput(
      'detail',
      detail({ profile: { ...detail().profile, display_name: 'Paper — renamed elsewhere' } }),
    );
    await rendered.fixture.whenStable();

    expect((screen.getByLabelText('Profile name') as HTMLInputElement).value).toBe(
      'Paper — renamed elsewhere',
    );
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
    expect(screen.getByRole('button', { name: 'Save next revision' })).toBeTruthy();
  });
});
