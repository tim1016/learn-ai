import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';

import type {
  AccountEffectivePosture,
  AccountRosterRow,
  AccountServiceAttachment,
  AccountServicePhase,
  AccountServiceStatusResponse,
  AccountsRosterResponse,
} from '../../../api/account-directory.types';
import type { AccountTriageVerdictState } from '../../../api/account-reconciliation.types';
import { BrokerService } from '../../../services/broker.service';

interface RosterState {
  readonly response: AccountsRosterResponse | null;
  readonly loading: boolean;
  readonly errorMessage: string | null;
}

interface ServiceStatusState {
  readonly response: AccountServiceStatusResponse | null;
  readonly loading: boolean;
  readonly errorMessage: string | null;
}

const EMPTY_ROSTER: RosterState = { response: null, loading: false, errorMessage: null };
const EMPTY_STATUS: ServiceStatusState = { response: null, loading: false, errorMessage: null };

/**
 * Route-scoped Account desk directory state. It validates only backend-owned
 * roster and service projections; it never derives posture or attachment.
 */
@Injectable()
export class AccountDeskDirectoryStore {
  private readonly broker = inject(BrokerService);
  private readonly destroyRef = inject(DestroyRef);
  private rosterGeneration = 0;
  private statusGeneration = 0;
  private readonly statusAccountKey = signal<string | null>(null);
  private readonly rosterState = signal<RosterState>(EMPTY_ROSTER);
  private readonly statusState = signal<ServiceStatusState>(EMPTY_STATUS);

  readonly rosterRows = computed(() => this.rosterState().response?.rows ?? []);
  readonly rosterLoading = computed(() => this.rosterState().loading);
  readonly rosterErrorMessage = computed(() => this.rosterState().errorMessage);
  readonly rosterHasLastGood = computed(() => this.rosterState().response !== null);
  readonly rosterShowingStaleLastGood = computed(() =>
    this.rosterState().response !== null && this.rosterState().errorMessage !== null,
  );
  readonly rosterEmpty = computed(() => this.rosterState().response?.rows.length === 0);
  readonly statusAccountId = this.statusAccountKey.asReadonly();
  readonly serviceStatus = computed(() => this.statusState().response);
  readonly serviceStatusLoading = computed(() => this.statusState().loading);
  readonly serviceStatusErrorMessage = computed(() => this.statusState().errorMessage);
  readonly serviceStatusHasLastGood = computed(() => this.statusState().response !== null);
  readonly serviceStatusShowingStaleLastGood = computed(() =>
    this.statusState().response !== null && this.statusState().errorMessage !== null,
  );

  constructor() {
    this.destroyRef.onDestroy(() => {
      this.rosterGeneration += 1;
      this.statusGeneration += 1;
    });
  }

  async loadRoster(): Promise<void> {
    const generation = ++this.rosterGeneration;
    this.rosterState.update((state) => ({ ...state, loading: true, errorMessage: null }));
    try {
      const response = await this.broker.accounts();
      if (generation !== this.rosterGeneration) return;
      if (!isAccountsRosterResponse(response)) throw new Error('Account roster response was malformed.');
      this.rosterState.set({ response, loading: false, errorMessage: null });
    } catch (error) {
      if (generation === this.rosterGeneration) {
        this.rosterState.update((state) => ({
          ...state,
          loading: false,
          errorMessage: serverMessage(error, 'Account roster is unavailable. Retry to request it again.'),
        }));
      }
    }
  }

  async loadServiceStatus(accountId: string): Promise<void> {
    if (this.statusAccountKey() !== accountId) {
      this.statusGeneration += 1;
      this.statusAccountKey.set(accountId);
      this.statusState.set(EMPTY_STATUS);
    }
    const generation = ++this.statusGeneration;
    this.statusState.update((state) => ({ ...state, loading: true, errorMessage: null }));
    try {
      const response = await this.broker.accountServiceStatus(accountId);
      if (!this.isCurrentStatusRequest(accountId, generation)) return;
      if (!isAccountServiceStatusResponse(response, accountId)) {
        throw new Error('Account service response did not attest this route.');
      }
      this.statusState.set({ response, loading: false, errorMessage: null });
    } catch (error) {
      if (this.isCurrentStatusRequest(accountId, generation)) {
        this.statusState.update((state) => ({
          ...state,
          loading: false,
          errorMessage: serverMessage(error, 'Account service status is unavailable. Retry to request it again.'),
        }));
      }
    }
  }

  retryRoster(): void {
    void this.loadRoster();
  }

  retryServiceStatus(): void {
    const accountId = this.statusAccountKey();
    if (accountId !== null) void this.loadServiceStatus(accountId);
  }

  private isCurrentStatusRequest(accountId: string, generation: number): boolean {
    return this.statusAccountKey() === accountId && this.statusGeneration === generation;
  }
}

function isAccountsRosterResponse(value: unknown): value is AccountsRosterResponse {
  return isRecord(value) && value['schema_version'] === 1 && Array.isArray(value['rows']) &&
    value['rows'].every(isAccountRosterRow);
}

function isAccountRosterRow(value: unknown): value is AccountRosterRow {
  return isRecord(value) &&
    typeof value['account_id'] === 'string' &&
    value['broker'] === 'IBKR' &&
    isEffectivePosture(value['effective_posture']) &&
    isAccountServiceSummary(value['service']) &&
    isRosterVerdictSummary(value['latest_verdict_summary']) &&
    isNullableInt64Ms(value['last_verified_at_ms']);
}

function isAccountServiceStatusResponse(value: unknown, accountId: string): value is AccountServiceStatusResponse {
  return isRecord(value) &&
    value['schema_version'] === 1 &&
    value['account_id'] === accountId &&
    isAttachment(value['attachment']) &&
    isNullablePhase(value['phase']) &&
    isNullableGeneration(value['generation']) &&
    isNullableInt64Ms(value['generation_recorded_at_ms']) &&
    isNullableString(value['source']) &&
    isBinding(value['binding']) &&
    isNullableLease(value['lease']) &&
    isJournalWatermark(value['journal']);
}

function isAccountServiceSummary(value: unknown): boolean {
  return isRecord(value) &&
    isAttachment(value['attachment']) &&
    isNullablePhase(value['phase']) &&
    isNullableGeneration(value['generation']);
}

function isRosterVerdictSummary(value: unknown): boolean {
  return isRecord(value) &&
    isTriageVerdictState(value['state']) &&
    typeof value['headline'] === 'string' &&
    isInt64Ms(value['generated_at_ms']);
}

function isBinding(value: unknown): boolean {
  return isRecord(value) &&
    isAttachment(value['state']) &&
    isNullableGeneration(value['generation']) &&
    isNullableGeneration(value['lease_generation']);
}

function isNullableLease(value: unknown): boolean {
  return value === null || (
    isRecord(value) &&
    (value['status'] === 'RUNNING' || value['status'] === 'DRAINING') &&
    isGeneration(value['generation']) &&
    isInt64Ms(value['started_at_ms']) &&
    isInt64Ms(value['renewed_at_ms']) &&
    isInt64Ms(value['valid_until_ms'])
  );
}

function isJournalWatermark(value: unknown): boolean {
  return isRecord(value) &&
    isNullableGeneration(value['last_seq']) &&
    isNullableInt64Ms(value['last_write_ms']);
}

function isEffectivePosture(value: unknown): value is AccountEffectivePosture {
  return value === 'PAPER_EXECUTION' || value === 'UNSAFE' || value === 'UNKNOWN';
}

function isAttachment(value: unknown): value is AccountServiceAttachment {
  return value === 'ATTACHED' || value === 'UNATTACHED' || value === 'FENCED';
}

function isNullablePhase(value: unknown): value is AccountServicePhase | null {
  return value === null || value === 'accepting' || value === 'reconnecting' ||
    value === 'draining' || value === 'frozen';
}

function isTriageVerdictState(value: unknown): value is AccountTriageVerdictState {
  return value === 'FROZEN' || value === 'NOT_PROVEN' || value === 'NEEDS_ATTENTION' || value === 'CLEAN';
}

function isNullableGeneration(value: unknown): boolean {
  return value === null || isGeneration(value);
}

function isGeneration(value: unknown): boolean {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 1;
}

function isNullableInt64Ms(value: unknown): boolean {
  return value === null || isInt64Ms(value);
}

function isInt64Ms(value: unknown): boolean {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === 'string';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function serverMessage(error: unknown, fallback: string): string {
  if (!isRecord(error)) return fallback;
  const body = error['error'];
  if (!isRecord(body)) return fallback;
  const detail = body['detail'];
  return isRecord(detail) && typeof detail['message'] === 'string' ? detail['message'] : fallback;
}
