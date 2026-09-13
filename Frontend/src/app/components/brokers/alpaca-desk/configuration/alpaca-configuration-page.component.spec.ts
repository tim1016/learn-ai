import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { BehaviorSubject } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type {
  AlpacaDeskAccountChoice,
  AlpacaDeskState,
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

function deskChoice(
  overrides: Partial<AlpacaDeskAccountChoice> = {},
): AlpacaDeskAccountChoice {
  return {
    selection_id: 'profile-paper@1',
    profile_id: 'profile-paper',
    revision: 1,
    profile_label: 'Paper — strategy testing',
    account_id: 'PA000PAPER',
    nickname: null,
    account_label: 'Paper — strategy testing',
    endpoint_mode: 'paper',
    badge_label: 'Paper account',
    description: 'Paper — strategy testing uses the verified paper account.',
    action_kind: 'review_configuration',
    action_label: 'Review Paper — strategy testing',
    action_consequence: 'Review this saved revision without changing the running worker.',
    is_staged: false,
    is_effective: false,
    ...overrides,
  };
}

function deskState(overrides: Partial<AlpacaDeskState> = {}): AlpacaDeskState {
  return {
    activation_state: 'no_selection',
    headline: 'No Alpaca account is active for this installation',
    detail: 'Choose a verified account configuration for this worker.',
    lifecycle: [],
    selection_label: 'Choose an account configuration',
    consequence: 'Choosing here only opens the saved configuration for review.',
    action: { kind: 'review_configuration', label: 'Choose an account', enabled: true },
    selection_generation: 0,
    staged_choice: null,
    effective_choice: null,
    choices: [],
    empty_choices_message: null,
    profiles_requiring_setup: 0,
    setup_required_message: null,
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
  desk: AlpacaDeskState = deskState();
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
  readDeskState = vi.fn(async () => this.desk);
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
    // The backend reports the desk projection from the same generation as the
    // selection it just wrote; the fake mirrors that so the two reads agree.
    this.desk = { ...this.desk, selection_generation: generation + 1 };
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
    this.desk = { ...this.desk, selection_generation: generation + 1 };
    return this.current;
  });
}

async function renderPage(
  service: FakeConfigurationService,
  query: Record<string, string> = {},
) {
  const queryParamMap = new BehaviorSubject(convertToParamMap(query));
  const view = await render(AlpacaConfigurationPageComponent, {
    providers: [
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: {
          queryParamMap,
          snapshot: { queryParamMap: queryParamMap.value },
        },
      },
      { provide: BrokerConfigurationService, useValue: service },
    ],
  });
  return { ...view, queryParamMap };
}

/**
 * Writes stay disabled until the page has read both the selection and the
 * desk projection and their generations agree; wait for that before clicking.
 */
async function clickWhenWritesEnabled(name: string): Promise<void> {
  const button = await screen.findByRole('button', { name });
  await vi.waitFor(() => {
    if ((button as HTMLButtonElement).disabled) throw new Error('writes are still disabled');
  });
  await userEvent.click(button);
}

async function saveProfile(name: string): Promise<void> {
  await userEvent.click(await screen.findByRole('button', { name: 'New configuration profile' }));
  await userEvent.type(screen.getByLabelText('Profile name'), name);
  await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));
}

describe('AlpacaConfigurationPageComponent', () => {
  it('opens and highlights the exact revision selected from the desk without mutating it', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile());
    service.revisions.push(revision({ account_pin: 'PA000PAPER' }));

    await renderPage(service, { profileId: 'profile-paper', revision: '1' });

    expect(await screen.findByText('Selected from desk')).toBeTruthy();
    expect(screen.getByText(/Revision 1 was selected from the Alpaca desk/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Stage revision 1' })).toBeTruthy();
    expect(service.staged).toEqual([]);
    expect(service.applied).toEqual([]);
  });

  it('shows restart guidance when the selected revision already has Apply recorded', async () => {
    const service = new FakeConfigurationService();
    const staged = deskChoice({
      is_staged: true,
      action_kind: 'view_restart_steps',
      action_label: 'View restart steps',
    });
    service.profiles.push(profile());
    service.revisions.push(revision({ account_pin: 'PA000PAPER' }));
    service.current = selection({
      staged_profile_id: 'profile-paper',
      staged_revision: 1,
      apply_requested: true,
      apply_requested_at_ms: 1_757_000_500_000,
      apply_requested_generation: 2,
      selection_generation: 2,
    });
    service.desk = deskState({
      activation_state: 'apply_requested_restart_required',
      detail: 'Apply is recorded. A controlled restart is required.',
      consequence: 'The running worker has not changed.',
      staged_choice: staged,
      choices: [staged],
      selection_generation: 2,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });

    await renderPage(service, { profileId: 'profile-paper', revision: '1' });

    expect(await screen.findByText(/Revision 1 was selected from the Alpaca desk/)).toBeTruthy();
    expect(screen.getByText('Apply is recorded. A controlled restart is required.')).toBeTruthy();
    expect(screen.getByText(/The running worker has not changed/)).toBeTruthy();
    expect(screen.queryByText(/until you explicitly Stage and Apply/)).toBeNull();
    expect(service.staged).toEqual([]);
    expect(service.applied).toEqual([]);
  });

  it('renders backend guidance when the selected revision is already staged', async () => {
    const service = new FakeConfigurationService();
    const staged = deskChoice({
      is_staged: true,
      action_kind: 'review_staged_configuration',
      action_label: 'Review & apply Paper — strategy testing',
    });
    service.profiles.push(profile());
    service.revisions.push(revision({ account_pin: 'PA000PAPER' }));
    service.current = selection({
      staged_profile_id: 'profile-paper',
      staged_revision: 1,
      selection_generation: 1,
    });
    service.desk = deskState({
      activation_state: 'staged_not_applied',
      detail: 'Paper — strategy testing is staged. Review it before recording Apply.',
      consequence: 'Apply records the change for the next controlled worker restart.',
      staged_choice: staged,
      choices: [staged],
      selection_generation: 1,
      action: {
        kind: 'review_staged_configuration',
        label: 'Review & apply Paper — strategy testing',
        enabled: true,
      },
    });

    await renderPage(service, { profileId: 'profile-paper', revision: '1' });

    expect(await screen.findByText(/is staged. Review it before recording Apply/)).toBeTruthy();
    expect(screen.getByText(/Apply records the change/)).toBeTruthy();
    expect(screen.queryByText(/until you explicitly Stage and Apply/)).toBeNull();
  });

  it('does not render guidance from a different selection generation', async () => {
    const service = new FakeConfigurationService();
    const staged = deskChoice({
      is_staged: true,
      action_kind: 'view_restart_steps',
      action_label: 'View restart steps',
    });
    service.profiles.push(profile());
    service.revisions.push(revision({ account_pin: 'PA000PAPER' }));
    service.current = selection({
      staged_profile_id: 'profile-paper',
      staged_revision: 1,
      apply_requested: true,
      selection_generation: 3,
    });
    service.desk = deskState({
      activation_state: 'apply_requested_restart_required',
      detail: 'Stale restart guidance',
      consequence: 'Stale restart consequence',
      staged_choice: staged,
      choices: [staged],
      selection_generation: 2,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });

    await renderPage(service, { profileId: 'profile-paper', revision: '1' });

    expect(await screen.findByText(/Revision 1 was selected from the Alpaca desk/)).toBeTruthy();
    expect(screen.queryByText('Stale restart guidance')).toBeNull();
    expect(screen.queryByText('Stale restart consequence')).toBeNull();
    expect(screen.queryByText(/until you explicitly Stage and Apply/)).toBeNull();
  });

  it('follows a new exact revision when the desk changes the query on the same route', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(
      profile(),
      profile({ profile_id: 'profile-live', display_name: 'Live — guarded' }),
    );
    service.revisions.push(
      revision({ account_pin: 'PA000PAPER' }),
      revision({
        profile_id: 'profile-live',
        revision: 2,
        endpoint_mode: 'live',
        account_pin: 'LIVE0001',
      }),
    );

    const view = await renderPage(service, { profileId: 'profile-paper', revision: '1' });
    expect(await screen.findByText(/Revision 1 was selected from the Alpaca desk/)).toBeTruthy();

    view.queryParamMap.next(convertToParamMap({ profileId: 'profile-live', revision: '2' }));

    expect(await screen.findByText(/Revision 2 was selected from the Alpaca desk/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Stage revision 2' })).toBeTruthy();
    expect(service.readProfile).toHaveBeenCalledWith('profile-live');
    expect(service.staged).toEqual([]);
    expect(service.applied).toEqual([]);
  });

  it.each([
    'listProfiles',
    'listCredentialSlots',
    'listNicknames',
    'listRevisions',
    'readDeskState',
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
    await clickWhenWritesEnabled('Stage');

    expect(service.staged).toEqual([{ profileId: 'profile-1', revision: 1, generation: 0 }]);

    await clickWhenWritesEnabled('Apply staged revision');

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
    // Same generation as the selection read, or the page's stale-generation
    // gating correctly disables Stage.
    service.desk = deskState({ selection_generation: 8 });
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

    await clickWhenWritesEnabled('Stage revision 1');

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

    await clickWhenWritesEnabled('Stage');

    // The code-like reason arrives through the shared `receiptLabel` pipe.
    expect(screen.getByText('Selection Generation Conflict')).toBeTruthy();
    expect(screen.getByText('The selection changed while this page was open.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reload configuration' })).toBeTruthy();
    expect(service.stageSelection).toHaveBeenCalledTimes(1);
  });

  it('disables Stage and Apply while the desk lifecycle and selection generations disagree', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    service.current = selection({
      staged_profile_id: 'profile-1',
      staged_revision: 1,
      selection_generation: 4,
    });
    // The desk projection was read before another writer moved the fence.
    service.readDeskState = vi.fn(async () => ({
      ...service.desk,
      lifecycle: [
        { key: 'effective_configuration', label: 'Paper is active', status: 'complete', status_label: 'Complete' },
        { key: 'selected_configuration', label: 'Select configuration', status: 'current', status_label: 'Current step' },
        { key: 'worker_handoff', label: 'Restart worker', status: 'pending', status_label: 'Pending' },
      ],
      selection_generation: 3,
    }));
    await renderPage(service);

    expect(await screen.findByText('Refreshing / state unknown')).toBeTruthy();
    expect(screen.queryByText('Restart worker')).toBeNull();
    expect((await screen.findByRole('button', { name: 'Stage' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Apply staged revision' }) as HTMLButtonElement).disabled).toBe(true);
    expect(service.stageSelection).not.toHaveBeenCalled();
    expect(service.applySelection).not.toHaveBeenCalled();
  });

  it('renames durably rather than only in the row it was typed in', async () => {
    const service = new FakeConfigurationService();
    service.profiles.push(profile({ profile_id: 'profile-1' }));
    service.revisions.push(revision({ profile_id: 'profile-1' }));
    service.current = selection({
      staged_profile_id: 'profile-1',
      staged_revision: 1,
      selection_generation: 4,
    });
    service.desk = deskState({
      activation_state: 'staged_not_applied',
      detail: 'Paper — strategy testing is staged. Review it before recording Apply.',
      consequence: 'Apply records the change for the next controlled worker restart.',
      action: {
        kind: 'review_staged_configuration',
        label: 'Review & apply Paper — strategy testing',
        enabled: true,
      },
      selection_generation: 4,
      staged_choice: deskChoice({
        profile_id: 'profile-1',
        selection_id: 'profile-1@1',
        is_staged: true,
        action_kind: 'review_staged_configuration',
        action_label: 'Review & apply Paper — strategy testing',
        action_consequence: 'Apply records the change for the next controlled worker restart.',
      }),
    });
    await renderPage(service, { profileId: 'profile-1', revision: '1' });

    expect(await screen.findByText(/Paper — strategy testing is staged/)).toBeTruthy();
    const nameField = await screen.findByLabelText('Profile name');
    await userEvent.clear(nameField);
    await userEvent.type(nameField, 'Paper — overnight');
    service.desk = deskState({
      ...service.desk,
      detail: 'Paper — overnight is staged. Review it before recording Apply.',
      staged_choice: deskChoice({
        profile_id: 'profile-1',
        selection_id: 'profile-1@1',
        profile_label: 'Paper — overnight',
        account_label: 'Paper — overnight',
        is_staged: true,
        action_kind: 'review_staged_configuration',
        action_label: 'Review & apply Paper — overnight',
        action_consequence: 'Apply records the change for the next controlled worker restart.',
      }),
    });
    await userEvent.click(screen.getByRole('button', { name: 'Rename' }));

    expect(service.updateProfile).toHaveBeenCalledWith('profile-1', {
      displayName: 'Paper — overnight',
    });
    expect(service.profiles[0].display_name).toBe('Paper — overnight');
    expect(await screen.findByText(/Paper — overnight is staged/)).toBeTruthy();
    expect(service.readDeskState).toHaveBeenCalledTimes(2);
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
    await userEvent.click(screen.getByRole('button', { name: 'Save next revision' }));
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
