import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import type {
  BrokerAccountNickname,
  BrokerCredentialSlot,
  BrokerInstallationSelection,
  BrokerObservedAccount,
  BrokerProfile,
  BrokerProfileDetail,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';
import { AlpacaConfigurationPageComponent } from './alpaca-configuration-page.component';
import { BrokerConfigurationService } from './broker-configuration.service';

const SLOTS: readonly BrokerCredentialSlot[] = [
  { slot: 'default', label: 'Default credentials', available: true },
  { slot: 'live', label: 'Live credentials', available: false },
];

function revision(overrides: Partial<BrokerProfileRevision> = {}): BrokerProfileRevision {
  return {
    profile_id: 'profile-paper',
    revision: 1,
    schema_version: 1,
    credential_slot: 'default',
    endpoint_mode: 'paper',
    account_pin: null,
    account_pinned_at_ms: null,
    live_envelope: null,
    content_sha256: 'c'.repeat(64),
    complete: true,
    author_owner_id: 'owner-1',
    created_at_ms: 1_757_000_000_000,
    ...overrides,
  };
}

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
    selection_generation: 0,
    effective_profile_id: null,
    effective_revision: null,
    effective_account_id: null,
    effective_acknowledged_at_ms: null,
    last_apply_outcome: null,
    last_apply_refusal_reason: null,
    ...overrides,
  };
}

/**
 * A stand-in for the whole configuration surface, holding the state the real
 * service persists so a test can reload the page against it.
 */
class FakeConfigurationService {
  profiles: BrokerProfile[] = [];
  revisions: BrokerProfileRevision[] = [];
  nicknames: BrokerAccountNickname[] = [];
  current: BrokerInstallationSelection = selection();
  observed: readonly BrokerObservedAccount[] = [
    { account_id: 'PA3ZK9QWERTY', account_mode: 'paper', account_status: 'ACTIVE' },
  ];
  readonly staged: { profileId: string; revision: number; generation: number }[] = [];
  readonly applied: number[] = [];
  stageRefusal: HttpErrorResponse | null = null;

  listCredentialSlots = vi.fn(async () => SLOTS);
  listNicknames = vi.fn(async () => this.nicknames);
  listProfiles = vi.fn(async (options: { includeArchived?: boolean } = {}) =>
    this.profiles.filter((entry) => options.includeArchived === true || !entry.archived),
  );
  listRevisions = vi.fn(async () => this.revisions);
  readSelection = vi.fn(async () => this.current);

  readRevision = vi.fn(async (profileId: string, rev: number) => {
    const found = this.revisions.find(
      (entry) => entry.profile_id === profileId && entry.revision === rev,
    );
    if (found === undefined) throw new Error(`no revision ${profileId}@${rev}`);
    return found;
  });

  readProfile = vi.fn(async (profileId: string): Promise<BrokerProfileDetail> => {
    const found = this.profiles.find((entry) => entry.profile_id === profileId);
    if (found === undefined) throw new Error(`no profile ${profileId}`);
    const latest = this.revisions.filter((entry) => entry.profile_id === profileId).at(-1) ?? null;
    return { profile: found, latest_revision: latest };
  });

  createProfile = vi.fn(async (displayName: string): Promise<BrokerProfileDetail> => {
    const created = profile({ profile_id: `profile-${this.profiles.length + 1}`, display_name: displayName });
    this.profiles.push(created);
    const first = revision({ profile_id: created.profile_id });
    this.revisions.push(first);
    return { profile: created, latest_revision: first };
  });

  updateProfile = vi.fn(async (profileId: string, patch: { displayName?: string; archived?: boolean }) => {
    const index = this.profiles.findIndex((entry) => entry.profile_id === profileId);
    const updated = {
      ...this.profiles[index],
      ...(patch.displayName === undefined ? {} : { display_name: patch.displayName }),
      ...(patch.archived === undefined ? {} : { archived: patch.archived }),
    };
    this.profiles[index] = updated;
    return updated;
  });

  cloneProfile = vi.fn(async () => ({ profile: this.profiles[0], latest_revision: null }));
  createRevision = vi.fn(async () => revision({ revision: 2 }));
  verifyAccount = vi.fn(async () => this.observed);
  pinAccount = vi.fn(async () => revision({ account_pin: 'PA3ZK9QWERTY' }));
  putNickname = vi.fn(async () => ({ account_id: 'PA3ZK9QWERTY', nickname: 'x', updated_at_ms: 1 }));

  stageSelection = vi.fn(async (profileId: string, rev: number, generation: number) => {
    if (this.stageRefusal !== null) throw this.stageRefusal;
    this.staged.push({ profileId, revision: rev, generation });
    this.current = {
      ...this.current,
      staged_profile_id: profileId,
      staged_revision: rev,
      selection_generation: generation + 1,
    };
    return this.current;
  });

  applySelection = vi.fn(async (generation: number) => {
    this.applied.push(generation);
    this.current = {
      ...this.current,
      apply_requested: true,
      apply_requested_at_ms: 1_757_000_500_000,
      selection_generation: generation + 1,
    };
    return this.current;
  });
}

async function renderPage(service: FakeConfigurationService) {
  return render(AlpacaConfigurationPageComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerConfigurationService, useValue: service },
    ],
  });
}

async function saveProfile(name: string): Promise<void> {
  await userEvent.click(await screen.findByRole('button', { name: 'New configuration profile' }));
  await userEvent.type(screen.getByLabelText('Profile name'), name);
  await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));
}

describe('AlpacaConfigurationPageComponent', () => {
  it.each([
    'listProfiles',
    'listCredentialSlots',
    'listNicknames',
    'listRevisions',
    'readRevision',
  ] as const)('keeps configuration readable and offers Retry when %s fails', async (failedRead) => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile());
    service.revisions.push(revision({ account_pin: 'PA000PAPER' }));
    service.current = selection({
      effective_profile_id: 'profile-paper',
      effective_revision: 1,
      effective_account_id: 'PA000PAPER',
    });
    service[failedRead].mockRejectedValue(new Error('Configuration read unavailable'));
    await renderPage(service);

    if (failedRead !== 'listProfiles') {
      await userEvent.click(await screen.findByText('Paper — strategy testing', { exact: true }));
      expect(await screen.findByRole('heading', { name: 'Open profile' })).toBeTruthy();
    }

    expect(await screen.findByRole('heading', { name: 'Installation selection' })).toBeTruthy();
    expect((await screen.findAllByRole('button', { name: 'Retry' })).length).toBeGreaterThan(0);
    if (failedRead === 'listRevisions') {
      expect(screen.getByText('Revision history (not read)')).toBeTruthy();
    }
    if (failedRead === 'readRevision') {
      expect(screen.getByText('endpoint unread')).toBeTruthy();
    }
  });

  it('reloads the unavailable endpoint after a failed revision read', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile());
    service.revisions.push(revision());
    service.current = selection({ effective_profile_id: 'profile-paper', effective_revision: 1 });
    service.readRevision.mockRejectedValueOnce(new Error('Revision read unavailable'));
    await renderPage(service);

    expect(await screen.findByText('endpoint unread')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('Paper')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('saves a named profile, survives a reload, stages it, and applies it', async () => {
    const service = new FakeConfigurationService();
    const first = await renderPage(service);
    await saveProfile('Paper — strategy testing');
    expect(await screen.findByText('Paper — strategy testing')).toBeTruthy();

    // "Reload the page" is a fresh component against the same stored state.
    first.fixture.destroy();
    TestBed.resetTestingModule();
    await renderPage(service);
    await userEvent.click(await screen.findByRole('button', { name: 'Stage' }));

    expect(service.staged).toEqual([{ profileId: 'profile-1', revision: 1, generation: 0 }]);

    await userEvent.click(screen.getByRole('button', { name: 'Apply staged revision' }));

    expect(service.applied).toEqual([1]);
    expect(screen.getByText(/nothing has changed yet/)).toBeTruthy();
  });

  it.each([false, true])('stages an exact historical revision with conflict=%s', async (conflict) => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile());
    service.revisions.push(revision(), revision({ revision: 2 }), revision({ revision: 3, complete: false }));
    service.current = selection({
      effective_profile_id: 'profile-paper',
      effective_revision: 1,
      staged_profile_id: 'profile-paper',
      staged_revision: 2,
      selection_generation: 8,
      last_apply_outcome: 'refused',
      last_apply_refusal_reason: 'The prior account has outstanding obligations.',
    });
    if (conflict) {
      service.stageRefusal = new HttpErrorResponse({
        status: 409,
        error: { detail: {
          reason: 'selection_generation_conflict',
          message: 'Another tab changed the selection.',
          next_step: 'Reload the configuration and stage again.',
        } },
      });
    }
    await renderPage(service);
    await userEvent.click(await screen.findByText('Paper — strategy testing', { exact: true }));
    await userEvent.click(await screen.findByText('Revision history (3)'));
    expect((screen.getByRole('button', { name: 'Stage revision 3' }) as HTMLButtonElement).disabled).toBe(true);

    await userEvent.click(screen.getByRole('button', { name: 'Stage revision 1' }));

    expect(service.stageSelection).toHaveBeenCalledExactlyOnceWith('profile-paper', 1, 8);
    expect(service.applied).toEqual([]);
    expect(service.current.effective_revision).toBe(1);
    if (conflict) {
      expect(service.current.staged_revision).toBe(2);
      expect(await screen.findByRole('button', { name: 'Reload configuration' })).toBeTruthy();
    } else {
      expect(service.current.staged_revision).toBe(1);
    }
  });

  it('surfaces a newer tab’s write as a reload, and never retries it', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    service.stageRefusal = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          reason: 'selection_generation_conflict',
          message: 'The selection changed while this page was open.',
          next_step: 'Reload the configuration and stage again.',
        },
      },
    });
    await renderPage(service);

    await userEvent.click(await screen.findByRole('button', { name: 'Stage' }));

    // The code-like reason arrives through the shared `receiptLabel` pipe.
    expect(screen.getByText('Selection Generation Conflict')).toBeTruthy();
    expect(screen.getByText('The selection changed while this page was open.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reload configuration' })).toBeTruthy();
    expect(service.stageSelection).toHaveBeenCalledTimes(1);
  });

  it('renames durably rather than only in the row it was typed in', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    await renderPage(service);

    await userEvent.click(await screen.findByText('Paper — strategy testing'));
    const nameField = await screen.findByLabelText('Profile name');
    await userEvent.clear(nameField);
    await userEvent.type(nameField, 'Paper — overnight');
    await userEvent.click(screen.getByRole('button', { name: 'Rename' }));

    expect(service.updateProfile).toHaveBeenCalledWith('profile-1', {
      displayName: 'Paper — overnight',
    });
    expect(service.profiles[0].display_name).toBe('Paper — overnight');
  });

  it('says what an unavailable credential slot means and who can fix it', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1', credential_slot: 'live' }));
    await renderPage(service);

    await userEvent.click(await screen.findByText('Paper — strategy testing'));

    expect(await screen.findByText(/No credential pair is injected for this slot/)).toBeTruthy();
    expect(screen.getByText(/this page cannot make it/)).toBeTruthy();
  });

  it('offers no credential field anywhere on the page', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    await renderPage(service);
    await userEvent.click(await screen.findByText('Paper — strategy testing'));

    expect(screen.queryAllByRole('textbox', { name: /key|secret|password/i })).toHaveLength(0);
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0);
  });

  it('reads the effective revision so the status panel can say Live, and still never arms', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(
      revision({
        profile_id: 'profile-1',
        revision: 2,
        endpoint_mode: 'live',
        credential_slot: 'live',
        live_envelope: {
          loss_fraction: 0.05,
          loss_usd: 5000,
          shadow_sessions: 3,
          arming_max_sessions: 20,
          xh_entry_bps: 11,
          xh_exit_bps: 17.5,
        },
      }),
    );
    service.current = selection({
      effective_profile_id: 'profile-1',
      effective_revision: 2,
      effective_account_id: '9LIVE0001',
      selection_generation: 4,
    });
    await renderPage(service);

    expect(await screen.findByText('Live')).toBeTruthy();
    expect(service.readRevision).toHaveBeenCalledWith('profile-1', 2);
    expect(screen.getByText(/Apply never arms live trading/)).toBeTruthy();
  });

  it('keeps the endpoint on screen across an Apply, which changes neither side', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1', revision: 2, endpoint_mode: 'live' }));
    service.current = selection({
      staged_profile_id: 'profile-1',
      staged_revision: 2,
      effective_profile_id: 'profile-1',
      effective_revision: 2,
      selection_generation: 4,
    });
    const rendered = await renderPage(service);
    expect(await screen.findAllByText('Live')).toHaveLength(2);

    await userEvent.click(screen.getByRole('button', { name: 'Apply staged revision' }));
    await rendered.fixture.whenStable();

    // Apply replaces the selection object without changing either revision, so
    // a params identity change would blank both reads to "endpoint unread".
    expect(screen.queryByText('endpoint unread')).toBeNull();
    expect(screen.getAllByText('Live')).toHaveLength(2);
    expect(service.readRevision).toHaveBeenCalledTimes(2);
  });

  it('drops an observation the moment it stops describing the revision on screen', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    const rendered = await renderPage(service);
    await userEvent.click(await screen.findByText('Paper — strategy testing'));
    await userEvent.click(await screen.findByRole('button', { name: 'Verify account (read-only)' }));
    expect(await screen.findByRole('button', { name: 'Approve this account' })).toBeTruthy();

    // A new revision is now the latest; the observation belongs to the old one.
    service.revisions.push(revision({ profile_id: 'profile-1', revision: 2 }));
    await userEvent.click(screen.getByRole('button', { name: 'Save revision 2' }));
    await rendered.fixture.whenStable();

    expect(screen.queryByRole('button', { name: 'Approve this account' })).toBeNull();
  });

  it('shows no observation at all when a verification is refused', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    await renderPage(service);
    await userEvent.click(await screen.findByText('Paper — strategy testing'));
    await userEvent.click(await screen.findByRole('button', { name: 'Verify account (read-only)' }));
    expect(await screen.findByRole('button', { name: 'Approve this account' })).toBeTruthy();

    service.verifyAccount = vi.fn(async () => {
      throw new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason: 'credential_slot_unavailable',
            message: 'No credential pair is injected for this slot.',
            next_step: 'Inject the pair on the host and verify again.',
          },
        },
      });
    });
    await userEvent.click(screen.getByRole('button', { name: 'Verify account (read-only)' }));

    expect(screen.getByText('Credential Slot Unavailable')).toBeTruthy();
    // Stale accounts left beside a refusal read as "those are still current".
    expect(screen.queryByRole('button', { name: 'Approve this account' })).toBeNull();
  });

  it('does not claim no profiles are saved before the list has been read', async () => {
    const service = new FakeConfigurationService();
    let release = (): void => {};
    const pending = new Promise<void>((resolve) => {
      release = () => resolve();
    });
    service.listProfiles = vi.fn(async () => {
      await pending;
      return service.profiles;
    });
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    const rendered = await renderPage(service);

    expect(screen.getByText('Reading saved profiles…')).toBeTruthy();
    expect(screen.queryByText(/No configuration profiles are saved yet/)).toBeNull();

    release();
    await rendered.fixture.whenStable();

    expect(await screen.findByText('Paper — strategy testing')).toBeTruthy();
  });

  it('has no detectable accessibility violations with a profile open and a form expanded', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    await renderPage(service);
    await userEvent.click(await screen.findByText('Paper — strategy testing'));
    await userEvent.click(screen.getByRole('button', { name: 'New configuration profile' }));

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });

  it('approves only an account the broker was observed to reach', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    await renderPage(service);
    await userEvent.click(await screen.findByText('Paper — strategy testing'));

    await userEvent.click(await screen.findByRole('button', { name: 'Verify account (read-only)' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Approve this account' }));

    expect(service.pinAccount).toHaveBeenCalledWith('profile-1', 1, 'PA3ZK9QWERTY');
  });
});
