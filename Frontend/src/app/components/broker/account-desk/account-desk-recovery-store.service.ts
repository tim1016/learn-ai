import { Injectable, computed, inject, signal } from '@angular/core';

import type {
  AccountAcceptExposureOverrideResponse,
  AccountClearFreezeResponse,
  AccountReconciliationAutomationPolicy,
  AccountReconciliationReceipt,
} from '../../../api/account-reconciliation.types';
import type { OperatorConfirmationCopy } from '../../../api/operator-blocker.types';
import { BrokerService } from '../../../services/broker.service';
import type { OperatorBlockerMoveEvent } from '../shared/operator-blocker-list/operator-blocker-list.component';
import { AccountDeskEventsStore } from './account-desk-events-store.service';
import { AccountDeskSurfaceStore } from './account-desk-surface-store.service';

export type AccountDeskRecoveryCommand =
  | 'reconcile'
  | 'automation'
  | 'clear_freeze'
  | 'exposure_override';

export interface AccountDeskRecoveryConfirmation {
  readonly command: AccountDeskRecoveryCommand;
  readonly accountId: string;
  readonly title: string;
  readonly body: string;
  readonly consequence: string;
  readonly confirmLabel: string;
  readonly desiredAutomationEnabled: boolean | null;
  readonly reason: string;
}

export type AccountDeskRecoverySuccess =
  | { readonly kind: 'reconcile'; readonly receipt: AccountReconciliationReceipt }
  | { readonly kind: 'automation'; readonly policy: AccountReconciliationAutomationPolicy }
  | { readonly kind: 'clear_freeze'; readonly receipt: AccountClearFreezeResponse }
  | { readonly kind: 'exposure_override'; readonly receipt: AccountAcceptExposureOverrideResponse };

/**
 * Executes only account-desk actions explicitly declared by backend moves or
 * the current backend policy projection. It preserves returned receipts while
 * refreshing the route-scoped proof and event timeline after a success.
 */
@Injectable()
export class AccountDeskRecoveryStore {
  private readonly broker = inject(BrokerService);
  private readonly surface = inject(AccountDeskSurfaceStore);
  private readonly events = inject(AccountDeskEventsStore);
  private requestGeneration = 0;
  private readonly accountKey = signal<string | null>(null);
  private readonly confirmationState = signal<AccountDeskRecoveryConfirmation | null>(null);
  private readonly busyState = signal(false);
  private readonly errorMessageState = signal<string | null>(null);
  private readonly successState = signal<AccountDeskRecoverySuccess | null>(null);

  readonly confirmation = this.confirmationState.asReadonly();
  readonly busy = this.busyState.asReadonly();
  readonly errorMessage = this.errorMessageState.asReadonly();
  readonly success = this.successState.asReadonly();
  readonly canConfirm = computed(() => {
    const confirmation = this.confirmationState();
    return confirmation !== null &&
      (confirmation.command !== 'exposure_override' || confirmation.reason.trim().length > 0);
  });

  load(accountId: string): void {
    if (this.accountKey() === accountId) return;
    this.requestGeneration += 1;
    this.accountKey.set(accountId);
    this.confirmationState.set(null);
    this.busyState.set(false);
    this.errorMessageState.set(null);
    this.successState.set(null);
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
    this.openConfirmation(accountId, command, confirmation);
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
      desiredAutomationEnabled: desiredEnabled,
      reason: '',
    });
    this.errorMessageState.set(null);
  }

  setExposureOverrideReason(reason: string): void {
    const confirmation = this.confirmationState();
    if (confirmation?.command !== 'exposure_override') return;
    this.confirmationState.set({ ...confirmation, reason });
  }

  cancelConfirmation(): void {
    if (this.busyState()) return;
    this.confirmationState.set(null);
    this.errorMessageState.set(null);
  }

  async confirm(): Promise<void> {
    const confirmation = this.confirmationState();
    const generation = this.requestGeneration;
    if (confirmation === null || this.busyState() || !this.canConfirm()) return;
    this.busyState.set(true);
    this.errorMessageState.set(null);
    try {
      const success = await this.execute(confirmation);
      if (!this.isCurrent(confirmation.accountId, generation)) return;
      this.successState.set(success);
      this.confirmationState.set(null);
      await Promise.all([
        this.surface.load(confirmation.accountId),
        this.events.load(confirmation.accountId),
      ]);
    } catch (error) {
      if (this.isCurrent(confirmation.accountId, generation)) {
        this.errorMessageState.set(recoveryErrorMessage(error));
      }
    } finally {
      if (this.isCurrent(confirmation.accountId, generation)) this.busyState.set(false);
    }
  }

  private openConfirmation(
    accountId: string,
    command: Exclude<AccountDeskRecoveryCommand, 'automation'>,
    confirmation: OperatorConfirmationCopy,
  ): void {
    this.confirmationState.set({
      command,
      accountId,
      title: confirmation.title,
      body: confirmation.body,
      consequence: confirmation.consequence,
      confirmLabel: confirmation.confirm_label,
      desiredAutomationEnabled: null,
      reason: '',
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
    default:
      return null;
  }
}

function recoveryErrorMessage(error: unknown): string {
  if (!isRecord(error) || !isRecord(error['error']) || !isRecord(error['error']['detail'])) {
    return 'Account recovery was not accepted. Review the current proof and try again.';
  }
  const message = error['error']['detail']['message'];
  if (typeof message === 'string') return message;
  return 'Account recovery was not accepted. Review the current proof and try again.';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
