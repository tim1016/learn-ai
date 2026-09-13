import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';

import { commandBodyOf, type ResourceTarget, withCommand } from '../../../../fleet/resource-target';
import { laneUrl } from '../../../../fleet/clerk-scoped-url';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../../api/broker.types';
import type {
  AlpacaDeskState,
  BrokerAccountNickname,
  BrokerCredentialSlot,
  BrokerEndpointMode,
  BrokerInstallationSelection,
  BrokerLiveEnvelope,
  BrokerObservedAccount,
  BrokerProfile,
  BrokerProfileDetail,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';

/** The revision content a create or a new revision carries. Never a secret. */
export interface RevisionContent {
  readonly credential_slot: string;
  readonly endpoint_mode: BrokerEndpointMode;
  readonly live_envelope: BrokerLiveEnvelope | null;
}

/**
 * Read/write client for the user-owned broker configuration surface
 * (ADR 0060, contract §4) at the clerk-scoped Alpaca configuration route.
 *
 * Two things about this client are load-bearing:
 *
 * - **Relative `/api/...` URLs.** Every route here is a data-plane control
 *   surface — reads included — so the request must go through the dev proxy
 *   that attaches `X-Data-Plane-Control-Secret`. An absolute
 *   `environment.pythonServiceUrl` URL bypasses the proxy and 403s.
 * - **No read coalescing.** These reads are not polled, and every write
 *   carries an `expected_*` fence read from the last response. Joining an
 *   in-flight read issued before a write landed would hand the caller a stale
 *   generation, which is exactly what the fence exists to prevent — so this
 *   client deliberately does not go through `PolledReadScheduler`. Writes
 *   return the state they produced; callers adopt that rather than re-reading.
 *
 * Nothing here sends or receives key material: a profile names an opaque
 * credential *slot*, and only slot labels and availability cross the API.
 */
@Injectable({ providedIn: 'root' })
export class BrokerConfigurationService {
  private readonly http = inject(HttpClient);

  /** The clerk-scoped configuration base (FR-092): every read and command
   * names the lane it addresses; repair stays reachable for an unbound lane
   * because the surface is configuration-access readiness. */
  private base(clerkId: string): string {
    return laneUrl({ broker: 'alpaca', clerkId }, '/configuration');
  }

  /** Wrap one configuration command with the §10.3 envelope. Configuration
   * commands are one-shot: provider-side selection state fences them, so
   * only the capability is mandatory. */
  private commandBody(target: ResourceTarget, payload: object): object {
    return commandBodyOf(
      withCommand(target, 'configuration_manage', target.idempotencyKey),
      payload,
    );
  }

  private profileBase(clerkId: string, profileId: string): string {
    return `${this.base(clerkId)}/profiles/${encodeURIComponent(profileId)}`;
  }

  private revisionBase(clerkId: string, profileId: string, revision: number): string {
    return `${this.profileBase(clerkId, profileId)}/revisions/${encodeURIComponent(String(revision))}`;
  }

  /** Backend-authored activation guidance; reads durable configuration only. */
  readDeskState(clerkId: string): Promise<AlpacaDeskState> {
    return firstValueFrom(this.http.get<AlpacaDeskState>(`${this.base(clerkId)}/desk-state`));
  }

  listCredentialSlots(clerkId: string): Promise<readonly BrokerCredentialSlot[]> {
    return firstValueFrom(
      this.http.get<components['schemas']['CredentialSlotsResponse']>(`${this.base(clerkId)}/credential-slots`),
    ).then((response) => response.slots);
  }

  listProfiles(
    clerkId: string,
    options: { includeArchived?: boolean } = {},
  ): Promise<readonly BrokerProfile[]> {
    const params = new HttpParams().set('include_archived', options.includeArchived === true);
    return firstValueFrom(
      this.http.get<components['schemas']['ProfileListResponse']>(`${this.base(clerkId)}/profiles`, { params }),
    ).then((response) => response.profiles);
  }

  readProfile(clerkId: string, profileId: string): Promise<BrokerProfileDetail> {
    return firstValueFrom(this.http.get<BrokerProfileDetail>(this.profileBase(clerkId, profileId)));
  }

  createProfile(target: ResourceTarget, displayName: string, content: RevisionContent): Promise<BrokerProfileDetail> {
    return firstValueFrom(
      this.http.post<BrokerProfileDetail>(`${this.base(target.clerkId)}/profiles`, this.commandBody(target, {
        display_name: displayName,
        ...content,
      })),
    );
  }

  /** Metadata only. A rename never invalidates an arming (ADR 0060 Decision 4). */
  updateProfile(
    target: ResourceTarget,
    profileId: string,
    patch: { displayName?: string; archived?: boolean },
  ): Promise<BrokerProfile> {
    return firstValueFrom(
      this.http.patch<BrokerProfile>(
        this.profileBase(target.clerkId, profileId),
        this.commandBody(target, {
          ...(patch.displayName === undefined ? {} : { display_name: patch.displayName }),
          ...(patch.archived === undefined ? {} : { archived: patch.archived }),
        }),
      ),
    );
  }

  /** New profile, copied content, no account pin carried over (contract §4). */
  cloneProfile(target: ResourceTarget, profileId: string, displayName: string): Promise<BrokerProfileDetail> {
    return firstValueFrom(
      this.http.post<BrokerProfileDetail>(`${this.profileBase(target.clerkId, profileId)}/clone`, this.commandBody(target, {
        display_name: displayName,
      })),
    );
  }

  listRevisions(clerkId: string, profileId: string): Promise<readonly BrokerProfileRevision[]> {
    return firstValueFrom(
      this.http.get<components['schemas']['RevisionListResponse']>(
        `${this.profileBase(clerkId, profileId)}/revisions`,
      ),
    ).then((response) => response.revisions);
  }

  /**
   * Write the next immutable revision. `expectedRevision` is the stale-edit
   * fence: a mismatch is a `409 revision_conflict` that overwrites nothing.
   */
  createRevision(
    target: ResourceTarget,
    profileId: string,
    expectedRevision: number,
    content: RevisionContent,
  ): Promise<BrokerProfileRevision> {
    return firstValueFrom(
      this.http.post<BrokerProfileRevision>(
        `${this.profileBase(target.clerkId, profileId)}/revisions`,
        this.commandBody(target, {
          expected_revision: expectedRevision,
          ...content,
        }),
      ),
    );
  }

  /**
   * One exact revision. The staged and effective revisions are read this way
   * rather than through `readProfile`, which answers with a profile's *latest*
   * revision — a different revision whenever an edit has been saved since.
   */
  readRevision(clerkId: string, profileId: string, revision: number): Promise<BrokerProfileRevision> {
    return firstValueFrom(
      this.http.get<BrokerProfileRevision>(this.revisionBase(clerkId, profileId, revision)),
    );
  }

  /** Read-only broker account discovery. Submits and cancels nothing. */
  verifyAccount(
    target: ResourceTarget,
    profileId: string,
    revision: number,
  ): Promise<readonly BrokerObservedAccount[]> {
    return firstValueFrom(
      this.http.post<components['schemas']['AccountVerificationResponse']>(
        `${this.revisionBase(target.clerkId, profileId, revision)}/verify-account`,
        this.commandBody(target, {}),
      ),
    ).then((response) => response.observed_accounts);
  }

  /** Pin one account the operator selected from an observation. Never typed by hand. */
  pinAccount(
    target: ResourceTarget,
    profileId: string,
    revision: number,
    accountId: string,
  ): Promise<BrokerProfileRevision> {
    return firstValueFrom(
      this.http.post<BrokerProfileRevision>(
        `${this.revisionBase(target.clerkId, profileId, revision)}/account-pin`,
        this.commandBody(target, { account_id: accountId }),
      ),
    );
  }

  listNicknames(clerkId: string): Promise<readonly BrokerAccountNickname[]> {
    return firstValueFrom(
      this.http.get<components['schemas']['NicknameListResponse']>(`${this.base(clerkId)}/account-nicknames`),
    ).then((response) => response.nicknames);
  }

  /** A nickname is keyed to the observed account, so one account reads the same everywhere. */
  putNickname(target: ResourceTarget, accountId: string, nickname: string): Promise<BrokerAccountNickname> {
    return firstValueFrom(
      this.http.put<BrokerAccountNickname>(
        `${this.base(target.clerkId)}/account-nicknames/${encodeURIComponent(accountId)}`,
        this.commandBody(target, { nickname }),
      ),
    );
  }

  readSelection(clerkId: string): Promise<BrokerInstallationSelection> {
    return firstValueFrom(this.http.get<BrokerInstallationSelection>(`${this.base(clerkId)}/selection`));
  }

  /** Staging, not switching: it governs nothing until an Apply and a restart. */
  stageSelection(
    target: ResourceTarget,
    profileId: string,
    revision: number,
    expectedSelectionGeneration: number,
  ): Promise<BrokerInstallationSelection> {
    return firstValueFrom(
      this.http.put<BrokerInstallationSelection>(`${this.base(target.clerkId)}/selection`, this.commandBody(target, {
        profile_id: profileId,
        revision,
        expected_selection_generation: expectedSelectionGeneration,
      })),
    );
  }

  /**
   * Record the one-shot Apply request. Answers 202 and changes no runtime: the
   * staged revision becomes effective at the operator's next controlled
   * restart, and this page never restarts anything.
   */
  applySelection(target: ResourceTarget, expectedSelectionGeneration: number): Promise<BrokerInstallationSelection> {
    return firstValueFrom(
      this.http.post<BrokerInstallationSelection>(`${this.base(target.clerkId)}/selection/apply`, this.commandBody(target, {
        expected_selection_generation: expectedSelectionGeneration,
      })),
    );
  }
}
