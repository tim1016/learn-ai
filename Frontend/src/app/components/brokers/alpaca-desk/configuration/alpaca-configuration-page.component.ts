import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  inject,
  linkedSignal,
  resource,
  viewChild,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { resourceTarget, type ResourceTarget } from '../../../../fleet/resource-target';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';

import type {
  BrokerInstallationSelection,
  BrokerObservedAccount,
  BrokerProfile,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';
import {
  type ConfigurationRefusal,
  clientRefusal,
  toConfigurationRefusal,
} from './broker-configuration-refusal';
import { BrokerConfigurationService, type RevisionContent } from './broker-configuration.service';
import { ConfigurationHandoffScriptComponent } from './configuration-handoff-script.component';
import { ConfigurationLifecycleTrackerComponent } from './configuration-lifecycle-tracker.component';
import { ConfigurationProfileCreateComponent } from './configuration-profile-create.component';
import {
  ConfigurationProfileDetailComponent,
  type RevisionSubmission,
} from './configuration-profile-detail.component';
import { ConfigurationProfileListComponent } from './configuration-profile-list.component';
import { ConfigurationRefusalComponent } from './configuration-refusal.component';
import { ConfigurationStatusPanelComponent } from './configuration-status-panel.component';
import { ConfigurationSwitchGuideComponent } from './configuration-switch-guide.component';

/** The `(profile, revision)` pair one side of the selection names, if it names one. */
interface RevisionRef {
  readonly profileId: string;
  readonly revision: number;
}

interface ClerkRevisionRef extends RevisionRef {
  readonly clerkId: string;
}

function reviewRef(params: { get(name: string): string | null }): RevisionRef | null {
  const profileId = params.get('profileId');
  const revision = Number(params.get('revision'));
  return profileId === null || profileId.length === 0 || !Number.isInteger(revision) || revision < 1
    ? null
    : { profileId, revision };
}

function revisionRef(
  selection: BrokerInstallationSelection | null,
  side: 'staged' | 'effective',
): RevisionRef | undefined {
  const profileId = selection?.[`${side}_profile_id`] ?? null;
  const revision = selection?.[`${side}_revision`] ?? null;
  return profileId === null || revision === null ? undefined : { profileId, revision };
}

/** Two refs naming the same revision are the same ref, whatever their identity. */
function sameRevisionRef(a: RevisionRef | undefined, b: RevisionRef | undefined): boolean {
  return a?.profileId === b?.profileId && a?.revision === b?.revision;
}

function sameClerkRevisionRef(
  a: ClerkRevisionRef | undefined,
  b: ClerkRevisionRef | undefined,
): boolean {
  return a?.clerkId === b?.clerkId && sameRevisionRef(a, b);
}

/**
 * The saved-configuration surface for the Alpaca broker path (ADR 0060).
 *
 * It owns every read and every write on this page, so one place holds the
 * `selection_generation` and the `expected_revision` that fence a stale write.
 * Two rules follow from that and are the reason the mutations are not spread
 * across the child components:
 *
 * - **A write's response is the new truth.** `PUT /selection` and
 *   `POST /selection/apply` both answer with the selection they produced, so
 *   that value is adopted directly instead of re-read. A re-read could race a
 *   second tab and hand this page a generation that is already stale.
 * - **A conflict is never retried.** A `409` from either fence means another
 *   tab wrote first and nothing here was overwritten; the surface says so and
 *   offers a reload, which is the only correct next move.
 *
 * Nothing on this page restarts a worker, arms a live limit, or asks for a
 * credential. Applying records an intent; a controlled restart on the host is
 * what makes it effective.
 */
@Component({
  selector: 'app-alpaca-configuration-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ConfigurationProfileCreateComponent,
    ConfigurationProfileDetailComponent,
    ConfigurationProfileListComponent,
    ConfigurationHandoffScriptComponent,
    ConfigurationLifecycleTrackerComponent,
    ConfigurationRefusalComponent,
    ConfigurationStatusPanelComponent,
    ConfigurationSwitchGuideComponent,
    RouterLink,
  ],
  templateUrl: './alpaca-configuration-page.component.html',
  styleUrl: './alpaca-configuration-page.component.scss',
})
export class AlpacaConfigurationPageComponent {
  private readonly service = inject(BrokerConfigurationService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly route = inject(ActivatedRoute);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });
  /** The lane this configuration surface addresses (FR-092). */
  readonly clerkId = input.required<string>();
  private readonly requestedReview = computed(() => reviewRef(this.queryParams()));
  private readonly createForm = viewChild(ConfigurationProfileCreateComponent);
  private readonly selectionContext = computed(() => ({
    clerkId: this.clerkId(),
    request: this.requestedReview(),
  }));

  protected readonly includeArchived = linkedSignal<string, boolean>({
    source: () => this.clerkId(),
    computation: () => false,
  });
  protected readonly selectedProfileId = linkedSignal<
    { readonly clerkId: string; readonly request: RevisionRef | null },
    string | null
  >({
    source: this.selectionContext,
    computation: ({ request }) => request?.profileId ?? null,
  });
  protected readonly reviewRevision = computed(() => {
    const request = this.requestedReview();
    return request !== null && this.selectedProfileId() === request.profileId
      ? request.revision
      : null;
  });
  /**
   * The last verification, carrying the exact revision it was taken for.
   *
   * An observation is evidence about *one* revision's credential slot and
   * endpoint mode. Keying it to that revision — rather than clearing it on the
   * actions that happen to change profiles — is what stops an "approve this
   * account" button being offered for an account nobody observed under the
   * revision now on screen. Every path that could leave the two out of step is
   * then covered by construction, including the ones that forget to clear.
   */
  private readonly observation = linkedSignal<string, {
    readonly ref: RevisionRef;
    readonly accounts: readonly BrokerObservedAccount[];
  } | null>({
    source: () => this.clerkId(),
    computation: () => null,
  });
  protected readonly refusal = linkedSignal<string, ConfigurationRefusal | null>({
    source: () => this.clerkId(),
    computation: () => null,
  });
  protected readonly busy = linkedSignal<string, boolean>({
    source: () => this.clerkId(),
    computation: () => false,
  });

  protected readonly slots = resource({
    params: () => this.clerkId(),
    loader: ({ params }) => this.service.listCredentialSlots(params),
    defaultValue: [],
  });
  protected readonly profiles = resource({
    params: () => ({ clerkId: this.clerkId(), includeArchived: this.includeArchived() }),
    loader: ({ params }) => this.service.listProfiles(params.clerkId, {
      includeArchived: params.includeArchived,
    }),
    defaultValue: [],
  });
  protected readonly nicknames = resource({
    params: () => this.clerkId(),
    loader: ({ params }) => this.service.listNicknames(params),
    defaultValue: [],
  });
  protected readonly deskState = resource({
    params: () => this.clerkId(),
    loader: ({ params }) => this.service.readDeskState(params),
  });
  protected readonly selection = resource({
    params: () => this.clerkId(),
    loader: ({ params }) => this.service.readSelection(params),
  });
  protected readonly detail = resource({
    params: () => {
      const profileId = this.selectedProfileId();
      return profileId === null ? undefined : { clerkId: this.clerkId(), profileId };
    },
    loader: ({ params }) => this.service.readProfile(params.clerkId, params.profileId),
  });
  // No `defaultValue`: a revision list that failed to load must not render as
  // "Revision history (0)", which states a fact about a read that never landed.
  protected readonly revisions = resource({
    params: () => {
      const profileId = this.selectedProfileId();
      return profileId === null ? undefined : { clerkId: this.clerkId(), profileId };
    },
    loader: ({ params }) => this.service.listRevisions(params.clerkId, params.profileId),
  });
  // `GET /selection` names the staged and effective revisions but not what
  // they *are*, and paper-versus-live is the fact an operator most needs from
  // this page. Each is read as an exact revision: a profile's latest revision
  // is a different one the moment an edit is saved.
  //
  // Both refs are `computed`s carrying an explicit `equal`, for the same reason
  // the form drafts are: `resource.params` is wrapped in a computed using
  // `Object.is`, so a fresh-but-equal object literal counts as a params change.
  // Every write replaces the selection object, so without this an Apply — which
  // changes neither side — would restart both reads and blank the panel to
  // "endpoint unread" at exactly the moment the operator pressed the button.
  private readonly stagedRef = computed<ClerkRevisionRef | undefined>(() => {
    const ref = revisionRef(this.currentSelection(), 'staged');
    return ref === undefined ? undefined : { clerkId: this.clerkId(), ...ref };
  }, { equal: sameClerkRevisionRef });
  private readonly effectiveRef = computed<ClerkRevisionRef | undefined>(() => {
    const ref = revisionRef(this.currentSelection(), 'effective');
    return ref === undefined ? undefined : { clerkId: this.clerkId(), ...ref };
  }, { equal: sameClerkRevisionRef });
  private readonly stagedRevision = resource({
    params: () => this.stagedRef(),
    loader: ({ params }) => this.service.readRevision(
      params.clerkId,
      params.profileId,
      params.revision,
    ),
  });
  private readonly effectiveRevision = resource({
    params: () => this.effectiveRef(),
    loader: ({ params }) => this.service.readRevision(
      params.clerkId,
      params.profileId,
      params.revision,
    ),
  });

  protected readonly stagedEndpointMode = computed(
    () => this.stagedRevision.hasValue() ? this.stagedRevision.value().endpoint_mode : null,
  );
  protected readonly effectiveEndpointMode = computed(
    () => this.effectiveRevision.hasValue() ? this.effectiveRevision.value().endpoint_mode : null,
  );

  protected readonly supplementalReadFailed = computed(() => Boolean(
    this.nicknames.error() || this.revisions.error()
    || this.stagedRevision.error() || this.effectiveRevision.error()
    || this.deskState.error(),
  ));
  protected readonly requestedRevisionMissing = computed(() =>
    this.reviewRevision() !== null
      && this.revisions.hasValue()
      && !this.revisions.value().some((revision) => revision.revision === this.reviewRevision()),
  );

  protected readonly currentSelection = computed(() =>
    this.selection.hasValue() ? this.selection.value() : null,
  );

  /**
   * The desk lifecycle and the adopted selection must both be read and agree
   * on the selection generation before any write runs: another writer may
   * have moved the fence between the two reads, and an unread desk state
   * cannot confirm it didn't. Every write (Stage and Apply) stays disabled
   * until a newly adopted response and a fresh desk state agree — the tracker
   * shows the refreshing note in the meantime.
   */
  protected readonly writesBlocked = computed(() => {
    const state = this.deskState.hasValue() ? this.deskState.value() : null;
    const selection = this.currentSelection();
    return state === null || selection === null
      || state.selection_generation !== selection.selection_generation;
  });

  /** One disable signal for every write surface: busy, or generations disagree. */
  protected readonly writesDisabled = computed(() => this.busy() || this.writesBlocked());
  protected readonly reviewContext = computed(() => {
    const request = this.requestedReview();
    const revisionNumber = this.reviewRevision();
    if (request === null || revisionNumber === null) return null;
    const introduction = `Revision ${revisionNumber} was selected from the Alpaca desk for review.`;
    const selection = this.currentSelection();
    if (!this.deskState.hasValue() || selection === null) {
      return { introduction, detail: null, consequence: null };
    }
    const state = this.deskState.value();
    if (state.selection_generation !== selection.selection_generation) {
      return { introduction, detail: null, consequence: null };
    }
    const staged = state.staged_choice;
    if (
      state.action.kind !== 'review_configuration'
      && staged?.profile_id === request.profileId
      && staged.revision === request.revision
    ) {
      return { introduction, detail: state.detail, consequence: state.consequence };
    }
    return {
      introduction,
      detail: 'No configuration changes until you explicitly Stage and Apply.',
      consequence: null,
    };
  });

  /**
   * The profile this page has open, with its latest revision. Every write that
   * needs one asks here, so "a profile is open and its content has loaded" is
   * stated once rather than re-derived as a guard in each handler.
   */
  private readonly openProfileContext = computed(() => {
    const profileId = this.selectedProfileId();
    const detail = this.detail.hasValue() ? this.detail.value() : undefined;
    return profileId === null || detail === undefined
      ? null
      : { profileId, latestRevision: detail.latest_revision };
  });

  /** The nickname of the account the open revision is approved for, if one is set. */
  protected readonly detailNickname = computed(() =>
    this.nicknameFor(this.openProfileContext()?.latestRevision?.account_pin ?? null),
  );

  /** The nickname of the account the worker actually bound, if one is set. */
  protected readonly effectiveNickname = computed(() =>
    this.nicknameFor(this.currentSelection()?.effective_account_id ?? null),
  );

  /**
   * The observation, but only when it belongs to the revision now on screen.
   * A verification taken for another revision is not evidence about this one,
   * so it is not shown and its accounts offer no approve button.
   */
  protected readonly observedAccounts = computed(() => {
    const observation = this.observation();
    const target = this.verificationTarget();
    return observation !== null && target !== null && sameRevisionRef(observation.ref, target)
      ? observation.accounts
      : null;
  });

  protected openProfile(profileId: string): void {
    this.selectedProfileId.set(profileId === this.selectedProfileId() ? null : profileId);
  }

  protected reloadAll(): void {
    this.refusal.set(null);
    this.observation.set(null);
    this.slots.reload();
    this.profiles.reload();
    this.nicknames.reload();
    this.deskState.reload();
    this.selection.reload();
    this.detail.reload();
    this.revisions.reload();
    this.stagedRevision.reload();
    this.effectiveRevision.reload();
  }

  protected createProfile(request: { displayName: string; content: RevisionContent }): void {
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      const created = await this.service.createProfile(actionTarget, request.displayName, request.content);
      if (!this.isCurrent(actionTarget)) return;
      this.createForm()?.reset();
      this.selectedProfileId.set(created.profile.profile_id);
      this.profiles.reload();
      this.deskState.reload();
    });
  }

  protected stageProfile(profile: BrokerProfile): void {
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      const detail = await this.service.readProfile(actionTarget.clerkId, profile.profile_id);
      if (!this.isCurrent(actionTarget)) return;
      const revision = detail.latest_revision;
      if (revision === null) {
        this.refusal.set(
          clientRefusal(
            'This profile has no revision to stage yet.',
            'Open it and save a revision first.',
          ),
        );
        return;
      }
      await this.stageExactRevision(revision, actionTarget);
    });
  }

  protected stageRevision(revision: BrokerProfileRevision): void {
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, () => this.stageExactRevision(revision, actionTarget));
  }

  private async stageExactRevision(revision: BrokerProfileRevision, actionTarget: ResourceTarget): Promise<void> {
    const current = this.currentSelection();
    if (current === null) {
      this.refusal.set(
        clientRefusal(
          'The current selection has not loaded, so staging cannot name the generation it is writing against.',
          'Reload the configuration and stage again.',
        ),
      );
      return;
    }
    const updated = await this.service.stageSelection(
      actionTarget,
      revision.profile_id,
      revision.revision,
      current.selection_generation,
    );
    if (!this.isCurrent(actionTarget)) return;
    this.selection.set(updated);
    this.deskState.reload();
  }

  protected applySelection(): void {
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      const current = this.currentSelection();
      if (current === null) return;
      const updated = await this.service.applySelection(
        actionTarget,
        current.selection_generation,
      );
      if (!this.isCurrent(actionTarget)) return;
      this.selection.set(updated);
      this.deskState.reload();
    });
  }

  protected setArchived(request: { profileId: string; archived: boolean }): void {
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      await this.service.updateProfile(actionTarget, request.profileId, { archived: request.archived });
      if (!this.isCurrent(actionTarget)) return;
      this.profiles.reload();
      this.detail.reload();
      this.deskState.reload();
    });
  }

  protected renameProfile(displayName: string): void {
    const open = this.openProfileContext();
    if (open === null) return;
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      await this.service.updateProfile(actionTarget, open.profileId, { displayName });
      if (!this.isCurrent(actionTarget)) return;
      this.profiles.reload();
      this.detail.reload();
      this.deskState.reload();
    });
  }

  protected cloneProfile(displayName: string): void {
    const open = this.openProfileContext();
    if (open === null) return;
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      const cloned = await this.service.cloneProfile(actionTarget, open.profileId, displayName);
      if (!this.isCurrent(actionTarget)) return;
      this.selectedProfileId.set(cloned.profile.profile_id);
      this.profiles.reload();
      this.deskState.reload();
    });
  }

  protected saveRevision(submission: RevisionSubmission): void {
    const open = this.openProfileContext();
    if (open === null) return;
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      await this.service.createRevision(
        actionTarget,
        open.profileId,
        submission.expectedRevision,
        submission.content,
      );
      if (!this.isCurrent(actionTarget)) return;
      this.detail.reload();
      this.revisions.reload();
    });
  }

  protected verifyAccount(): void {
    const target = this.verificationTarget();
    if (target === null) return;
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      // Dropped before the call, not after it: a refused verification that left
      // the previous run's accounts on screen would show a refusal banner over
      // a list of still-approvable accounts, which reads as "those are current".
      this.observation.set(null);
      const accounts = await this.service.verifyAccount(
        actionTarget,
        target.profileId,
        target.revision,
      );
      if (!this.isCurrent(actionTarget)) return;
      this.observation.set({ ref: target, accounts });
    });
  }

  protected pinAccount(accountId: string): void {
    const target = this.verificationTarget();
    if (target === null) return;
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      await this.service.pinAccount(actionTarget, target.profileId, target.revision, accountId);
      if (!this.isCurrent(actionTarget)) return;
      this.detail.reload();
      this.revisions.reload();
      this.deskState.reload();
    });
  }

  protected saveNickname(nickname: string): void {
    const accountId = this.openProfileContext()?.latestRevision?.account_pin ?? null;
    if (accountId === null) return;
    const actionTarget = this.commandTarget();
    void this.run(actionTarget, async () => {
      await this.service.putNickname(actionTarget, accountId, nickname);
      if (!this.isCurrent(actionTarget)) return;
      this.nicknames.reload();
      this.deskState.reload();
    });
  }

  private verificationTarget(): RevisionRef | null {
    const open = this.openProfileContext();
    return open === null || open.latestRevision === null
      ? null
      : { profileId: open.profileId, revision: open.latestRevision.revision };
  }

  /** Freeze the explicit route's rendered lane at the point an action opens. */
  private commandTarget(): ResourceTarget {
    const clerkId = this.clerkId();
    const lane = this.fleetDirectory.lane('alpaca', clerkId);
    if (typeof globalThis.crypto?.randomUUID !== 'function') {
      throw new Error('This browser cannot create a durable request identity.');
    }
    return resourceTarget('alpaca', clerkId, {
      capability: 'configuration_manage',
      idempotencyKey: globalThis.crypto.randomUUID(),
      bindingGeneration: lane?.effective_binding_generation ?? null,
      routingEpoch: lane?.routing_epoch ?? null,
    });
  }

  private nicknameFor(accountId: string | null): string | null {
    if (accountId === null || !this.nicknames.hasValue()) return null;
    return this.nicknames.value().find((entry) => entry.account_id === accountId)?.nickname ?? null;
  }

  private isCurrent(target: ResourceTarget): boolean {
    return this.clerkId() === target.clerkId;
  }

  /**
   * One write at a time, and the refusal it produced. A rejected write is
   * rendered from the server's own words; nothing is retried, because every
   * write here carries a fence and a retry would either be a no-op or clobber
   * whatever moved the fence.
   */
  private async run(target: ResourceTarget, action: () => Promise<void>): Promise<void> {
    if (this.busy()) return;
    this.busy.set(true);
    this.refusal.set(null);
    try {
      await action();
    } catch (error) {
      if (this.isCurrent(target)) this.refusal.set(toConfigurationRefusal(error));
    } finally {
      if (this.isCurrent(target)) this.busy.set(false);
    }
  }
}
