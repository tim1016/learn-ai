import { Injectable, computed, inject, signal } from '@angular/core';

import type {
  AccountAcceptExposureOverrideResponse,
  AccountClearFreezeResponse,
  AccountEmergencyFlattenResponse,
  AccountEventSequenceRepairReceipt,
  AccountRecoveryFlattenCandidate,
  BindingLedgerBaselineReceipt,
  AccountReconciliationAutomationPolicy,
  AccountReconciliationReceipt,
  JournalCurePreview,
  JournalCureReceipt,
  JournalCureRequest,
  LegacyStaleClaimCandidate,
  LegacyStaleClaimRetirementReceipt,
  OperatorRecoveryFlattenResponse,
} from '../../../api/account-reconciliation.types';
import type { AccountClerkRestoreReceipt, JournalRecoveryReceipt } from '../../../api/account-cockpit.types';
import type { OperatorConfirmationCopy } from '../../../api/operator-blocker.types';
import { BrokerService } from '../../../services/broker.service';
import { extractServerMessage } from '../operation-error';
import type { OperatorBlockerMoveEvent } from '../shared/operator-blocker-list/operator-blocker-list.component';
import { AccountDeskEventsStore } from './account-desk-events-store.service';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

export type AccountDeskRecoveryCommand =
  | 'reconcile'
  | 'automation'
  | 'clear_freeze'
  | 'exposure_override'
  | 'journal_cure'
  | 'legacy_retire'
  | 'recovery_flatten'
  | 'emergency_flatten'
  | 'restore_clerk'
  | 'journal_recovery'
  | 'binding_ledger_baseline'
  | 'event_sequence_repair';

export interface AccountDeskRecoveryConfirmation {
  readonly command: AccountDeskRecoveryCommand;
  readonly accountId: string;
  readonly title: string;
  readonly body: string;
  readonly consequence: string;
  readonly confirmLabel: string;
  readonly requiredToken: string;
  readonly providedToken: string;
  readonly desiredAutomationEnabled: boolean | null;
  readonly reason: string;
  readonly journalCure: { readonly preview: JournalCurePreview; readonly request: JournalCureRequest } | null;
  readonly legacyCandidate: LegacyStaleClaimCandidate | null;
  readonly recoveryFlatten: AccountRecoveryFlattenCandidate | null;
  readonly emergencyOperationId: string | null;
  readonly restoreOperationId: string | null;
}

export type AccountDeskRecoverySuccess =
  | { readonly kind: 'reconcile'; readonly receipt: AccountReconciliationReceipt }
  | { readonly kind: 'automation'; readonly policy: AccountReconciliationAutomationPolicy }
  | { readonly kind: 'clear_freeze'; readonly receipt: AccountClearFreezeResponse }
  | { readonly kind: 'exposure_override'; readonly receipt: AccountAcceptExposureOverrideResponse }
  | { readonly kind: 'journal_cure'; readonly receipt: JournalCureReceipt }
  | { readonly kind: 'legacy_retire'; readonly receipt: LegacyStaleClaimRetirementReceipt }
  | { readonly kind: 'recovery_flatten'; readonly receipt: OperatorRecoveryFlattenResponse }
  | { readonly kind: 'emergency_flatten'; readonly receipt: AccountEmergencyFlattenResponse }
  | { readonly kind: 'restore_clerk'; readonly receipt: AccountClerkRestoreReceipt }
  | { readonly kind: 'journal_recovery'; readonly receipt: JournalRecoveryReceipt }
  | { readonly kind: 'binding_ledger_baseline'; readonly receipt: BindingLedgerBaselineReceipt }
  | { readonly kind: 'event_sequence_repair'; readonly receipt: AccountEventSequenceRepairReceipt };

/**
 * Executes only exact account-desk requests declared by backend projections.
 * It preserves returned receipts while refreshing route-scoped proof, event
 * history, and cure candidates after a success.
 */
@Injectable()
export class AccountDeskRecoveryStore {
  private readonly broker = inject(BrokerService);
  private readonly surface = inject(AccountDeskSurfaceStore);
  private readonly events = inject(AccountDeskEventsStore);
  private readonly directory = inject(AccountDeskDirectoryStore);
  private requestGeneration = 0;
  private readonly accountKey = signal<string | null>(null);
  private readonly confirmationState = signal<AccountDeskRecoveryConfirmation | null>(null);
  private readonly busyState = signal(false);
  private readonly errorMessageState = signal<string | null>(null);
  private readonly successState = signal<AccountDeskRecoverySuccess | null>(null);
  private readonly legacyCandidatesState = signal<readonly LegacyStaleClaimCandidate[]>([]);
  private readonly legacyLoadingState = signal(false);
  private readonly legacyErrorMessageState = signal<string | null>(null);

  readonly confirmation = this.confirmationState.asReadonly();
  readonly busy = this.busyState.asReadonly();
  readonly errorMessage = this.errorMessageState.asReadonly();
  readonly success = this.successState.asReadonly();
  readonly legacyCandidates = this.legacyCandidatesState.asReadonly();
  readonly legacyLoading = this.legacyLoadingState.asReadonly();
  readonly legacyErrorMessage = this.legacyErrorMessageState.asReadonly();
  readonly canConfirm = computed(() => {
    const confirmation = this.confirmationState();
    return confirmation !== null &&
      (confirmation.requiredToken === '' || confirmation.providedToken === confirmation.requiredToken) &&
      (confirmation.command !== 'exposure_override' || confirmation.reason.trim().length > 0);
  });

  load(accountId: string): void {
    if (this.accountKey() === accountId) return;
    this.requestGeneration += 1;
    const generation = this.requestGeneration;
    this.accountKey.set(accountId);
    this.confirmationState.set(null);
    this.busyState.set(false);
    this.errorMessageState.set(null);
    this.successState.set(null);
    this.legacyCandidatesState.set([]);
    this.legacyLoadingState.set(false);
    this.legacyErrorMessageState.set(null);
    void this.loadLegacyCandidates(accountId, generation);
  }

  requestDeclaredMove(event: OperatorBlockerMoveEvent): void {
    const action = event.move.action;
    const confirmation = event.move.confirmation;
    const command = action.kind === 'confirm_in_form' ? recoveryCommandForAnchor(action.anchor) : null;
    const accountId = this.accountKey();
    if (
      this.busyState() ||
      event.blocker.host !== 'account_desk' ||
      command === null ||
      confirmation === undefined ||
      confirmation === null ||
      accountId === null
    ) return;
    if (command === 'recovery_flatten') {
      const candidate = this.surface.triage()?.recovery_flatten_candidates.find(
        (value) => value.intent.intent_id === event.move.target && value.intent.account_id === accountId,
      );
      if (candidate === undefined) return;
      this.openConfirmation(accountId, command, candidate.confirmation, { recoveryFlatten: candidate });
      return;
    }
    this.openConfirmation(accountId, command, confirmation);
  }

  requestCockpitMove(event: OperatorBlockerMoveEvent): void {
    const accountId = this.accountKey();
    const confirmation = event.move.confirmation;
    const cockpit = this.directory.cockpit();
    if (
      this.busyState() ||
      accountId === null ||
      cockpit === null ||
      (cockpit.mode !== 'CLERK_DOWN' && cockpit.mode !== 'JOURNAL_CORRUPT') ||
      event.move.action.kind !== 'confirm_in_form' ||
      (event.move.action.anchor !== 'account-clerk-restore-action' && event.move.action.anchor !== 'account-journal-recovery-action') ||
      confirmation === null ||
      confirmation === undefined ||
      !cockpit.blockers.some((blocker) => blocker.condition.id === event.blocker.condition.id)
    ) return;
    this.openConfirmation(accountId, cockpit.mode === 'JOURNAL_CORRUPT' ? 'journal_recovery' : 'restore_clerk', confirmation);
  }

  requestAutomationChange(policy: AccountReconciliationAutomationPolicy): void {
    const accountId = this.accountKey();
    if (this.busyState() || accountId === null || policy.account_id !== accountId) return;
    const desiredEnabled = !policy.enabled;
    this.confirmationState.set({
      command: 'automation',
      accountId,
      title: 'Confirm auto-reconcile change',
      body: `Change auto-reconcile after bot trades to ${desiredEnabled ? 'enabled' : 'disabled'}.`,
      consequence: 'The server will record the policy change in the account event journal.',
      confirmLabel: desiredEnabled ? 'Enable auto-reconcile' : 'Disable auto-reconcile',
      requiredToken: '',
      providedToken: '',
      desiredAutomationEnabled: desiredEnabled,
      reason: '',
      journalCure: null,
      legacyCandidate: null,
      recoveryFlatten: null,
      emergencyOperationId: null,
      restoreOperationId: null,
    });
    this.errorMessageState.set(null);
  }

  requestJournalCure(
    preview: JournalCurePreview,
    signedQuantity: number,
    reason: string,
    evidenceRef: string,
  ): void {
    const accountId = this.accountKey();
    const confirmation = preview.confirmation;
    if (
      this.busyState() ||
      accountId === null ||
      preview.account_id !== accountId ||
      !preview.can_cure ||
      confirmation === null ||
      !Number.isFinite(signedQuantity) ||
      signedQuantity === 0 ||
      reason.trim().length === 0 ||
      evidenceRef.trim().length === 0
    ) return;
    this.openConfirmation(accountId, 'journal_cure', confirmation, {
      journalCure: {
        preview,
        request: {
          bot_order_namespace: preview.bot_order_namespace,
          symbol: preview.symbol,
          signed_quantity: signedQuantity,
          reason: reason.trim(),
          evidence_refs: [evidenceRef.trim()],
          request_provenance: 'account-desk/journal-cure',
          idempotency_key: crypto.randomUUID(),
        },
      },
    });
  }

  requestLegacyRetirement(candidate: LegacyStaleClaimCandidate): void {
    const accountId = this.accountKey();
    if (
      this.busyState() ||
      accountId === null ||
      !this.legacyCandidatesState().includes(candidate)
    ) return;
    this.openConfirmation(accountId, 'legacy_retire', candidate.confirmation, { legacyCandidate: candidate });
  }

  requestBindingLedgerBaseline(): void {
    const accountId = this.accountKey();
    if (this.busyState() || accountId === null) return;
    this.confirmationState.set({
      command: 'binding_ledger_baseline',
      accountId,
      title: 'Repair binding ledger',
      body: 'Seed the Clerk binding ledger from the current account registry to clear dirty parity so bot admission is no longer fail-closed.',
      consequence:
        'The Account service records one binding decision per registry binding it does not already match. It never removes rows and leaves genuine ledger-only anomalies visible.',
      confirmLabel: 'Repair binding ledger',
      requiredToken: '',
      providedToken: '',
      desiredAutomationEnabled: null,
      reason: '',
      journalCure: null,
      legacyCandidate: null,
      recoveryFlatten: null,
      emergencyOperationId: null,
      restoreOperationId: null,
    });
    this.errorMessageState.set(null);
  }

  requestEventSequenceRepair(): void {
    const accountId = this.accountKey();
    if (this.busyState() || accountId === null) return;
    this.confirmationState.set({
      command: 'event_sequence_repair',
      accountId,
      title: 'Repair event history',
      body: 'Restore contiguous event-sequence numbers for an event history whose durable sequence envelope was duplicated.',
      consequence:
        'The original bytes are snapshotted beside the ledger before only the sequence field is rewritten. Malformed or cross-account rows are refused, never dropped.',
      confirmLabel: 'Repair event history',
      requiredToken: '',
      providedToken: '',
      desiredAutomationEnabled: null,
      reason: '',
      journalCure: null,
      legacyCandidate: null,
      recoveryFlatten: null,
      emergencyOperationId: null,
      restoreOperationId: null,
    });
    this.errorMessageState.set(null);
  }

  requestEmergencyFlatten(confirmation: OperatorConfirmationCopy): void {
    const accountId = this.accountKey();
    if (this.busyState() || accountId === null || confirmation.required_token !== 'FLATTEN') return;
    this.openConfirmation(accountId, 'emergency_flatten', confirmation);
  }

  refreshLegacyCandidates(): void {
    const accountId = this.accountKey();
    if (accountId !== null && !this.busyState()) void this.loadLegacyCandidates(accountId, this.requestGeneration);
  }

  setExposureOverrideReason(reason: string): void {
    const confirmation = this.confirmationState();
    if (confirmation?.command !== 'exposure_override') return;
    this.confirmationState.set({ ...confirmation, reason });
  }

  setConfirmationToken(token: string): void {
    const confirmation = this.confirmationState();
    if (confirmation === null || confirmation.requiredToken === '') return;
    this.confirmationState.set({ ...confirmation, providedToken: token });
  }

  cancelConfirmation(): void {
    if (this.busyState()) return;
    this.confirmationState.set(null);
    this.errorMessageState.set(null);
  }

  async confirm(): Promise<void> {
    const confirmation = this.confirmationState();
    const generation = this.requestGeneration;
    if (confirmation === null || this.busyState()) return;
    if (!this.canConfirm()) return;
    this.busyState.set(true);
    this.errorMessageState.set(null);
    let success: AccountDeskRecoverySuccess;
    try {
      success = await this.execute(confirmation);
    } catch (error) {
      if (this.isCurrent(confirmation.accountId, generation)) {
        this.errorMessageState.set(
          recoveryErrorMessage(error, 'Account recovery was not accepted. Review the current proof and try again.'),
        );
        this.busyState.set(false);
      }
      return;
    }
    if (!this.isCurrent(confirmation.accountId, generation)) return;
    this.successState.set(success);
    this.confirmationState.set(null);
    try {
      await Promise.all([
        this.surface.load(confirmation.accountId),
        this.events.load(confirmation.accountId),
        this.directory.loadServiceStatus(confirmation.accountId),
        this.loadLegacyCandidates(confirmation.accountId, generation),
      ]);
    } catch {
      if (!this.isCurrent(confirmation.accountId, generation)) return;
      this.errorMessageState.set('Account recovery was accepted, but fresh desk evidence is unavailable. Retry to refresh it.');
    } finally {
      if (this.isCurrent(confirmation.accountId, generation)) this.busyState.set(false);
    }
  }

  private openConfirmation(
    accountId: string,
    command: Exclude<AccountDeskRecoveryCommand, 'automation'>,
    confirmation: OperatorConfirmationCopy,
    details: {
      readonly journalCure?: { readonly preview: JournalCurePreview; readonly request: JournalCureRequest };
      readonly legacyCandidate?: LegacyStaleClaimCandidate;
      readonly recoveryFlatten?: AccountRecoveryFlattenCandidate;
    } = {},
  ): void {
    this.confirmationState.set({
      command,
      accountId,
      title: confirmation.title,
      body: confirmation.body,
      consequence: confirmation.consequence,
      confirmLabel: confirmation.confirm_label,
      requiredToken: confirmation.required_token,
      providedToken: '',
      desiredAutomationEnabled: null,
      reason: '',
      journalCure: details.journalCure ?? null,
      legacyCandidate: details.legacyCandidate ?? null,
      recoveryFlatten: details.recoveryFlatten ?? null,
      emergencyOperationId: command === 'emergency_flatten' ? crypto.randomUUID() : null,
      restoreOperationId: (command === 'restore_clerk' || command === 'journal_recovery') ? crypto.randomUUID() : null,
    });
    this.errorMessageState.set(null);
  }

  private async execute(confirmation: AccountDeskRecoveryConfirmation): Promise<AccountDeskRecoverySuccess> {
    switch (confirmation.command) {
      case 'reconcile':
        return { kind: 'reconcile', receipt: await this.broker.reconcileAccount(confirmation.accountId) };
      case 'automation':
        return {
          kind: 'automation',
          policy: await this.broker.updateAccountReconciliationAutomation(confirmation.accountId, {
            enabled: confirmation.desiredAutomationEnabled === true,
            updated_by: 'account-desk.operator',
          }),
        };
      case 'clear_freeze':
        return {
          kind: 'clear_freeze',
          receipt: await this.broker.clearAccountFreeze(confirmation.accountId, {
            requested_by: 'account-desk.operator',
          }),
        };
      case 'exposure_override':
        return {
          kind: 'exposure_override',
          receipt: await this.broker.acceptExposureOverride(confirmation.accountId, {
            requested_by: 'account-desk.operator',
            reason: confirmation.reason.trim(),
          }),
        };
      case 'journal_cure':
        if (confirmation.journalCure === null) throw new Error('Journal cure confirmation is incomplete.');
        return {
          kind: 'journal_cure',
          receipt: await this.broker.applyJournalCure(confirmation.accountId, confirmation.journalCure.request),
        };
      case 'legacy_retire':
        if (confirmation.legacyCandidate === null) throw new Error('Legacy retirement confirmation is incomplete.');
        return {
          kind: 'legacy_retire',
          receipt: await this.broker.retireLegacyStaleClaim(confirmation.accountId, {
            strategy_instance_id: confirmation.legacyCandidate.strategy_instance_id,
            run_id: confirmation.legacyCandidate.run_id,
            symbol: confirmation.legacyCandidate.symbol,
            requested_by: 'account-desk.operator',
          }),
        };
      case 'recovery_flatten':
        if (confirmation.recoveryFlatten === null) throw new Error('Recovery flatten confirmation is incomplete.');
        return {
          kind: 'recovery_flatten',
          receipt: await this.broker.submitOperatorRecoveryFlatten(confirmation.accountId, {
            intent: confirmation.recoveryFlatten.intent,
            request_provenance: 'account-desk/recovery-flatten',
          }),
        };
      case 'emergency_flatten':
        if (confirmation.providedToken !== 'FLATTEN' || confirmation.emergencyOperationId === null) {
          throw new Error('Emergency flatten confirmation is incomplete.');
        }
        return {
          kind: 'emergency_flatten',
          receipt: await this.broker.emergencyFlattenAccount(confirmation.accountId, {
            account: confirmation.accountId,
            confirmation_token: 'FLATTEN',
            idempotency_key: confirmation.emergencyOperationId,
          }),
        };
      case 'restore_clerk':
        if (confirmation.providedToken !== 'RESTORE' || confirmation.restoreOperationId === null) {
          throw new Error('Clerk restore confirmation is incomplete.');
        }
        return {
          kind: 'restore_clerk',
          receipt: await this.broker.restoreAccountClerk(confirmation.accountId, {
            confirmation_token: 'RESTORE',
            idempotency_key: confirmation.restoreOperationId,
          }),
        };
      case 'journal_recovery':
        if (
          confirmation.restoreOperationId === null ||
          (confirmation.providedToken !== 'QUARANTINE' && confirmation.providedToken !== 'REBASELINE')
        ) throw new Error('Journal recovery confirmation is incomplete.');
        return {
          kind: 'journal_recovery',
          receipt: await this.broker.recoverAccountJournal(
            confirmation.accountId,
            confirmation.providedToken === 'QUARANTINE' ? 'quarantine' : 'rebaseline',
            { confirmation_token: confirmation.providedToken, idempotency_key: confirmation.restoreOperationId },
          ),
        };
      case 'binding_ledger_baseline':
        return {
          kind: 'binding_ledger_baseline',
          receipt: await this.broker.baselineBindingLedger(confirmation.accountId),
        };
      case 'event_sequence_repair':
        return {
          kind: 'event_sequence_repair',
          receipt: await this.broker.repairAccountEventSequence(confirmation.accountId),
        };
    }
  }

  private async loadLegacyCandidates(accountId: string, generation: number): Promise<void> {
    if (!this.isCurrent(accountId, generation)) return;
    this.legacyLoadingState.set(true);
    this.legacyErrorMessageState.set(null);
    try {
      const response = await this.broker.legacyStaleClaimCandidates(accountId);
      if (!this.isCurrent(accountId, generation)) return;
      this.legacyCandidatesState.set(response.account_id === accountId ? response.candidates : []);
    } catch (error) {
      if (this.isCurrent(accountId, generation)) {
        this.legacyCandidatesState.set([]);
        this.legacyErrorMessageState.set(
          recoveryErrorMessage(error, 'Account recovery was not accepted. Review the current proof and try again.'),
        );
      }
    } finally {
      if (this.isCurrent(accountId, generation)) this.legacyLoadingState.set(false);
    }
  }

  private isCurrent(accountId: string, generation: number): boolean {
    return this.accountKey() === accountId && this.requestGeneration === generation;
  }
}

function recoveryCommandForAnchor(anchor: string): Exclude<AccountDeskRecoveryCommand, 'automation'> | null {
  switch (anchor) {
    case 'account-reconciliation-action':
      return 'reconcile';
    case 'account-clear-freeze-action':
      return 'clear_freeze';
    case 'account-exposure-override-action':
      return 'exposure_override';
    case 'account-recovery-flatten-action':
      return 'recovery_flatten';
    default:
      return null;
  }
}

function recoveryErrorMessage(error: unknown, fallback: string): string {
  const message = extractServerMessage(error, fallback);
  return /x-data-plane-control-secret/i.test(message)
    ? 'The secure control connection is unavailable. Ask a platform operator to restore it, then try again.'
    : message;
}
