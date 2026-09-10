import { ChangeDetectionStrategy, Component, computed, inject, resource, signal, viewChild } from '@angular/core';
import { RouterLink } from '@angular/router';

import type {
  BrokerInstallationSelection,
  BrokerObservedAccount,
  BrokerProfile,
} from '../../../../api/alpaca.types';
import {
  type ConfigurationRefusal,
  clientRefusal,
  toConfigurationRefusal,
} from './broker-configuration-refusal';
import { BrokerConfigurationService, type RevisionContent } from './broker-configuration.service';
import { ConfigurationProfileCreateComponent } from './configuration-profile-create.component';
import {
  ConfigurationProfileDetailComponent,
  type RevisionSubmission,
} from './configuration-profile-detail.component';
import { ConfigurationProfileListComponent } from './configuration-profile-list.component';
import { ConfigurationRefusalComponent } from './configuration-refusal.component';
import { ConfigurationStatusPanelComponent } from './configuration-status-panel.component';

/** The `(profile, revision)` pair one side of the selection names, if it names one. */
function revisionRef(
  selection: BrokerInstallationSelection | null,
  side: 'staged' | 'effective',
): { profileId: string; revision: number } | undefined {
  const profileId = selection?.[`${side}_profile_id`] ?? null;
  const revision = selection?.[`${side}_revision`] ?? null;
  return profileId === null || revision === null ? undefined : { profileId, revision };
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
    ConfigurationRefusalComponent,
    ConfigurationStatusPanelComponent,
    RouterLink,
  ],
  templateUrl: './alpaca-configuration-page.component.html',
  styleUrl: './alpaca-configuration-page.component.scss',
})
export class AlpacaConfigurationPageComponent {
  private readonly service = inject(BrokerConfigurationService);
  private readonly createForm = viewChild(ConfigurationProfileCreateComponent);

  protected readonly includeArchived = signal(false);
  protected readonly selectedProfileId = signal<string | null>(null);
  protected readonly observedAccounts = signal<readonly BrokerObservedAccount[] | null>(null);
  protected readonly refusal = signal<ConfigurationRefusal | null>(null);
  protected readonly busy = signal(false);

  protected readonly slots = resource({
    loader: () => this.service.listCredentialSlots(),
    defaultValue: [],
  });
  protected readonly profiles = resource({
    params: () => this.includeArchived(),
    loader: ({ params }) => this.service.listProfiles({ includeArchived: params }),
    defaultValue: [],
  });
  protected readonly nicknames = resource({
    loader: () => this.service.listNicknames(),
    defaultValue: [],
  });
  protected readonly selection = resource({ loader: () => this.service.readSelection() });
  protected readonly detail = resource({
    params: () => this.selectedProfileId() ?? undefined,
    loader: ({ params }) => this.service.readProfile(params),
  });
  protected readonly revisions = resource({
    params: () => this.selectedProfileId() ?? undefined,
    loader: ({ params }) => this.service.listRevisions(params),
    defaultValue: [],
  });
  // `GET /selection` names the staged and effective revisions but not what
  // they *are*, and paper-versus-live is the fact an operator most needs from
  // this page. Each is read as an exact revision: a profile's latest revision
  // is a different one the moment an edit is saved.
  private readonly stagedRevision = resource({
    params: () => revisionRef(this.currentSelection(), 'staged'),
    loader: ({ params }) => this.service.readRevision(params.profileId, params.revision),
  });
  private readonly effectiveRevision = resource({
    params: () => revisionRef(this.currentSelection(), 'effective'),
    loader: ({ params }) => this.service.readRevision(params.profileId, params.revision),
  });

  protected readonly stagedEndpointMode = computed(
    () => this.stagedRevision.value()?.endpoint_mode ?? null,
  );
  protected readonly effectiveEndpointMode = computed(
    () => this.effectiveRevision.value()?.endpoint_mode ?? null,
  );

  protected readonly currentSelection = computed(() =>
    this.selection.hasValue() ? this.selection.value() : null,
  );

  /**
   * The profile this page has open, with its latest revision. Every write that
   * needs one asks here, so "a profile is open and its content has loaded" is
   * stated once rather than re-derived as a guard in each handler.
   */
  private readonly openProfileContext = computed(() => {
    const profileId = this.selectedProfileId();
    const detail = this.detail.value();
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

  protected openProfile(profileId: string): void {
    this.selectedProfileId.set(profileId === this.selectedProfileId() ? null : profileId);
    // An observation belongs to the revision it was taken for; carrying it to
    // another profile would offer an "approve this account" button for an
    // account nobody observed under that profile's credentials.
    this.observedAccounts.set(null);
  }

  protected reloadAll(): void {
    this.refusal.set(null);
    this.observedAccounts.set(null);
    this.slots.reload();
    this.profiles.reload();
    this.nicknames.reload();
    this.selection.reload();
    this.detail.reload();
    this.revisions.reload();
    this.stagedRevision.reload();
    this.effectiveRevision.reload();
  }

  protected createProfile(request: { displayName: string; content: RevisionContent }): void {
    void this.run(async () => {
      const created = await this.service.createProfile(request.displayName, request.content);
      this.createForm()?.reset();
      this.selectedProfileId.set(created.profile.profile_id);
      this.profiles.reload();
    });
  }

  protected stageProfile(profile: BrokerProfile): void {
    void this.run(async () => {
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
      const detail = await this.service.readProfile(profile.profile_id);
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
      this.selection.set(
        await this.service.stageSelection(
          profile.profile_id,
          revision.revision,
          current.selection_generation,
        ),
      );
    });
  }

  protected applySelection(): void {
    void this.run(async () => {
      const current = this.currentSelection();
      if (current === null) return;
      this.selection.set(await this.service.applySelection(current.selection_generation));
    });
  }

  protected setArchived(request: { profileId: string; archived: boolean }): void {
    void this.run(async () => {
      await this.service.updateProfile(request.profileId, { archived: request.archived });
      this.profiles.reload();
      this.detail.reload();
    });
  }

  protected renameProfile(displayName: string): void {
    const open = this.openProfileContext();
    if (open === null) return;
    void this.run(async () => {
      await this.service.updateProfile(open.profileId, { displayName });
      this.profiles.reload();
      this.detail.reload();
    });
  }

  protected cloneProfile(displayName: string): void {
    const open = this.openProfileContext();
    if (open === null) return;
    void this.run(async () => {
      const cloned = await this.service.cloneProfile(open.profileId, displayName);
      this.selectedProfileId.set(cloned.profile.profile_id);
      this.observedAccounts.set(null);
      this.profiles.reload();
    });
  }

  protected saveRevision(submission: RevisionSubmission): void {
    const open = this.openProfileContext();
    if (open === null) return;
    void this.run(async () => {
      await this.service.createRevision(
        open.profileId,
        submission.expectedRevision,
        submission.content,
      );
      // The new revision carries no approved account, so the previous
      // revision's observation is no longer evidence about what is open.
      this.observedAccounts.set(null);
      this.detail.reload();
      this.revisions.reload();
    });
  }

  protected verifyAccount(): void {
    const target = this.verificationTarget();
    if (target === null) return;
    void this.run(async () => {
      this.observedAccounts.set(
        await this.service.verifyAccount(target.profileId, target.revision),
      );
    });
  }

  protected pinAccount(accountId: string): void {
    const target = this.verificationTarget();
    if (target === null) return;
    void this.run(async () => {
      await this.service.pinAccount(target.profileId, target.revision, accountId);
      this.detail.reload();
      this.revisions.reload();
    });
  }

  protected saveNickname(nickname: string): void {
    const accountId = this.openProfileContext()?.latestRevision?.account_pin ?? null;
    if (accountId === null) return;
    void this.run(async () => {
      await this.service.putNickname(accountId, nickname);
      this.nicknames.reload();
    });
  }

  private verificationTarget(): { profileId: string; revision: number } | null {
    const open = this.openProfileContext();
    return open === null || open.latestRevision === null
      ? null
      : { profileId: open.profileId, revision: open.latestRevision.revision };
  }

  private nicknameFor(accountId: string | null): string | null {
    if (accountId === null) return null;
    return this.nicknames.value().find((entry) => entry.account_id === accountId)?.nickname ?? null;
  }

  /**
   * One write at a time, and the refusal it produced. A rejected write is
   * rendered from the server's own words; nothing is retried, because every
   * write here carries a fence and a retry would either be a no-op or clobber
   * whatever moved the fence.
   */
  private async run(action: () => Promise<void>): Promise<void> {
    if (this.busy()) return;
    this.busy.set(true);
    this.refusal.set(null);
    try {
      await action();
    } catch (error) {
      this.refusal.set(toConfigurationRefusal(error));
    } finally {
      this.busy.set(false);
    }
  }
}
