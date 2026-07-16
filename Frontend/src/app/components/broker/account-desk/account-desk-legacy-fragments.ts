import type { AccountDeskLens } from '../../../api/operator-blocker.types';

/** A one-time bridge from Account Monitor anchors to Account Desk semantics. */
export interface AccountDeskFragmentTarget {
  readonly lens: AccountDeskLens;
  readonly anchor: string;
}

const RECOVERY_CONTROLS: AccountDeskFragmentTarget = {
  lens: 'operator',
  anchor: 'account-desk-recovery-controls',
};
const OPERATIONS_PROOF: AccountDeskFragmentTarget = {
  lens: 'operator',
  anchor: 'account-desk-operations-proof',
};
const ACCOUNT_SERVICE: AccountDeskFragmentTarget = {
  lens: 'operator',
  anchor: 'account-desk-account-service',
};
const VERDICT: AccountDeskFragmentTarget = {
  lens: 'trader',
  anchor: 'account-desk-verdict',
};

const LEGACY_ACCOUNT_MONITOR_FRAGMENT_TARGETS: Readonly<Record<string, AccountDeskFragmentTarget>> = {
  'account-primary-action-title': RECOVERY_CONTROLS,
  'account-outcome-title': OPERATIONS_PROOF,
  'account-observation-title': OPERATIONS_PROOF,
  'account-clerk-title': ACCOUNT_SERVICE,
  'account-reconciliation-action': RECOVERY_CONTROLS,
  'reconciliation-automation-help': RECOVERY_CONTROLS,
};

const ACCOUNT_DESK_FRAGMENT_TARGETS: Readonly<Record<string, AccountDeskFragmentTarget>> = {
  [RECOVERY_CONTROLS.anchor]: RECOVERY_CONTROLS,
  [OPERATIONS_PROOF.anchor]: OPERATIONS_PROOF,
  [ACCOUNT_SERVICE.anchor]: ACCOUNT_SERVICE,
  [VERDICT.anchor]: VERDICT,
};

export function legacyAccountMonitorFragmentTarget(
  fragment: string | null,
): AccountDeskFragmentTarget | null {
  return fragment === null ? null : (LEGACY_ACCOUNT_MONITOR_FRAGMENT_TARGETS[fragment] ?? null);
}

/** Resolves only known semantic anchors; all other fragments intentionally do nothing. */
export function accountDeskFragmentTarget(fragment: string | null): AccountDeskFragmentTarget | null {
  return fragment === null ? null : (ACCOUNT_DESK_FRAGMENT_TARGETS[fragment] ?? null);
}
