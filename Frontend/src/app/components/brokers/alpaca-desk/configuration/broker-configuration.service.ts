import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { components } from '../../../../api/broker.types';
import type {
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
 * (ADR 0060, contract §4) at `/api/brokers/alpaca/configuration`.
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
  private readonly base = '/api/brokers/alpaca/configuration';

  private profileBase(profileId: string): string {
    return `${this.base}/profiles/${encodeURIComponent(profileId)}`;
  }

  private revisionBase(profileId: string, revision: number): string {
    return `${this.profileBase(profileId)}/revisions/${encodeURIComponent(String(revision))}`;
  }

  listCredentialSlots(): Promise<readonly BrokerCredentialSlot[]> {
    return firstValueFrom(
      this.http.get<components['schemas']['CredentialSlotsResponse']>(`${this.base}/credential-slots`),
    ).then((response) => response.slots);
  }

  listProfiles(options: { includeArchived?: boolean } = {}): Promise<readonly BrokerProfile[]> {
    const params = new HttpParams().set('include_archived', options.includeArchived === true);
    return firstValueFrom(
      this.http.get<components['schemas']['ProfileListResponse']>(`${this.base}/profiles`, { params }),
    ).then((response) => response.profiles);
  }

  readProfile(profileId: string): Promise<BrokerProfileDetail> {
    return firstValueFrom(this.http.get<BrokerProfileDetail>(this.profileBase(profileId)));
  }

  createProfile(displayName: string, content: RevisionContent): Promise<BrokerProfileDetail> {
    return firstValueFrom(
      this.http.post<BrokerProfileDetail>(`${this.base}/profiles`, {
        display_name: displayName,
        ...content,
      }),
    );
  }

  /** Metadata only. A rename never invalidates an arming (ADR 0060 Decision 4). */
  updateProfile(
    profileId: string,
    patch: { displayName?: string; archived?: boolean },
  ): Promise<BrokerProfile> {
    return firstValueFrom(
      this.http.patch<BrokerProfile>(this.profileBase(profileId), {
        ...(patch.displayName === undefined ? {} : { display_name: patch.displayName }),
        ...(patch.archived === undefined ? {} : { archived: patch.archived }),
      }),
    );
  }

  /** New profile, copied content, no account pin carried over (contract §4). */
  cloneProfile(profileId: string, displayName: string): Promise<BrokerProfileDetail> {
    return firstValueFrom(
      this.http.post<BrokerProfileDetail>(`${this.profileBase(profileId)}/clone`, {
        display_name: displayName,
      }),
    );
  }

  listRevisions(profileId: string): Promise<readonly BrokerProfileRevision[]> {
    return firstValueFrom(
      this.http.get<components['schemas']['RevisionListResponse']>(
        `${this.profileBase(profileId)}/revisions`,
      ),
    ).then((response) => response.revisions);
  }

  /**
   * Write the next immutable revision. `expectedRevision` is the stale-edit
   * fence: a mismatch is a `409 revision_conflict` that overwrites nothing.
   */
  createRevision(
    profileId: string,
    expectedRevision: number,
    content: RevisionContent,
  ): Promise<BrokerProfileRevision> {
    return firstValueFrom(
      this.http.post<BrokerProfileRevision>(`${this.profileBase(profileId)}/revisions`, {
        expected_revision: expectedRevision,
        ...content,
      }),
    );
  }

  /** Read-only broker account discovery. Submits and cancels nothing. */
  verifyAccount(profileId: string, revision: number): Promise<readonly BrokerObservedAccount[]> {
    return firstValueFrom(
      this.http.post<components['schemas']['AccountVerificationResponse']>(
        `${this.revisionBase(profileId, revision)}/verify-account`,
        {},
      ),
    ).then((response) => response.observed_accounts);
  }

  /** Pin one account the operator selected from an observation. Never typed by hand. */
  pinAccount(
    profileId: string,
    revision: number,
    accountId: string,
  ): Promise<BrokerProfileRevision> {
    return firstValueFrom(
      this.http.post<BrokerProfileRevision>(`${this.revisionBase(profileId, revision)}/account-pin`, {
        account_id: accountId,
      }),
    );
  }

  listNicknames(): Promise<readonly BrokerAccountNickname[]> {
    return firstValueFrom(
      this.http.get<components['schemas']['NicknameListResponse']>(`${this.base}/account-nicknames`),
    ).then((response) => response.nicknames);
  }

  /** A nickname is keyed to the observed account, so one account reads the same everywhere. */
  putNickname(accountId: string, nickname: string): Promise<BrokerAccountNickname> {
    return firstValueFrom(
      this.http.put<BrokerAccountNickname>(
        `${this.base}/account-nicknames/${encodeURIComponent(accountId)}`,
        { nickname },
      ),
    );
  }

  readSelection(): Promise<BrokerInstallationSelection> {
    return firstValueFrom(this.http.get<BrokerInstallationSelection>(`${this.base}/selection`));
  }

  /** Staging, not switching: it governs nothing until an Apply and a restart. */
  stageSelection(
    profileId: string,
    revision: number,
    expectedSelectionGeneration: number,
  ): Promise<BrokerInstallationSelection> {
    return firstValueFrom(
      this.http.put<BrokerInstallationSelection>(`${this.base}/selection`, {
        profile_id: profileId,
        revision,
        expected_selection_generation: expectedSelectionGeneration,
      }),
    );
  }

  /**
   * Record the one-shot Apply request. Answers 202 and changes no runtime: the
   * staged revision becomes effective at the operator's next controlled
   * restart, and this page never restarts anything.
   */
  applySelection(expectedSelectionGeneration: number): Promise<BrokerInstallationSelection> {
    return firstValueFrom(
      this.http.post<BrokerInstallationSelection>(`${this.base}/selection/apply`, {
        expected_selection_generation: expectedSelectionGeneration,
      }),
    );
  }
}
