import type {
  AccountConditionRow,
  AccountCureAction,
} from '../../../api/account-reconciliation.types';
import type { BotLifecycleCondition } from '../../../api/live-instances.types';

export type AccountConditionActionKind = 'resolveExposure' | 'clearFreeze' | 'reconcile';
export type LifecycleConditionCureTarget = 'accountMonitor' | 'retireReplace';

const CONDITION_ACTION_LABELS: Record<AccountCureAction, string> = {
  resolve_exposure: 'Resolve exposure',
  clear_freeze: 'Clear freeze',
  reconcile_now: 'Run account reconcile',
  prove_evidence: 'Prove broker evidence',
  retire_replace: 'Retire & Replace',
};

export function conditionActionLabel(action: AccountCureAction): string {
  return CONDITION_ACTION_LABELS[action];
}

export function accountConditionActionKind(
  condition: Pick<AccountConditionRow, 'cure_action'>,
): AccountConditionActionKind | null {
  switch (condition.cure_action) {
    case 'resolve_exposure':
      return 'resolveExposure';
    case 'clear_freeze':
      return 'clearFreeze';
    case 'reconcile_now':
      return 'reconcile';
    case 'prove_evidence':
    case 'retire_replace':
      return null;
  }
}

export function lifecycleConditionCureTarget(
  condition: Pick<BotLifecycleCondition, 'cure_action'>,
): LifecycleConditionCureTarget {
  return condition.cure_action === 'retire_replace' ? 'retireReplace' : 'accountMonitor';
}
