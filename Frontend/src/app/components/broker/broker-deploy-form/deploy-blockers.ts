import type {
  BlockerSeverity,
  Disposition,
  OperatorBlocker,
  OperatorConditionScope,
  OperatorMove,
} from '../../../api/operator-blocker.types';

export interface FormBlockerInput {
  missingRequiredFields: string[];
  validationReceiptIssue?: string | null;
  brokerAccountState?: 'checking' | 'connected' | 'unavailable';
  deploymentNameError?: string | null;
  identityConflictSummary: string | null;
  exposureConflictSummary: string | null;
  customSizingError: string | null;
  dailyOrderLimitError?: string | null;
  allInCoexistenceBlock?: string | null;
  liveExecutionSelected?: boolean;
  actionPlanReady: boolean;
  actionPlanMessage?: string | null;
}

function confirmInForm(label: string, anchor: string): OperatorMove {
  return { label, action: { kind: 'confirm_in_form', anchor }, target: null };
}

function openStrategyValidation(): OperatorMove {
  return {
    label: 'Open Strategy Validation',
    action: { kind: 'navigate', route: '/strategy-validation', fragment: null },
    target: null,
  };
}

function openBrokerAccount(): OperatorMove {
  return {
    label: 'Open broker account',
    action: { kind: 'navigate', route: '/broker/account-monitor', fragment: null },
    target: null,
  };
}

function formBlocker(
  id: string,
  scope: OperatorConditionScope,
  headline: string,
  detail: string,
  move: OperatorMove | null,
  severity: BlockerSeverity = 'blocking',
  disposition: Disposition = 'fix_here',
): OperatorBlocker {
  return {
    condition: { id, severity, scope, evidence: {} },
    host: 'deploy_preflight',
    disposition,
    headline,
    detail,
    primary_move: move,
    secondary_moves: [],
    applies_to: 'deploy',
  };
}

export function buildFormBlockers(input: FormBlockerInput): OperatorBlocker[] {
  const blockers: OperatorBlocker[] = [];

  if (input.validationReceiptIssue) {
    blockers.push(
      formBlocker(
        'validated_receipt_unavailable',
        'strategy',
        'Validated strategy receipt is unavailable',
        input.validationReceiptIssue,
        openStrategyValidation(),
        'blocking',
        'fix_elsewhere',
      ),
    );
  }

  if (input.brokerAccountState === 'checking') {
    blockers.push(
      formBlocker(
        'broker_account_checking',
        'broker',
        'Checking the connected broker account',
        'Launch remains disabled until the Account Clerk can prove the connected broker account.',
        null,
        'blocking',
        'wait',
      ),
    );
  }

  if (input.brokerAccountState === 'unavailable') {
    blockers.push(
      formBlocker(
        'broker_account_unavailable',
        'broker',
        'Connected broker account unavailable',
        'Connect the broker session, then return to launch. The server derives the account from that session.',
        openBrokerAccount(),
        'blocking',
        'fix_elsewhere',
      ),
    );
  }

  if (input.missingRequiredFields.length > 0) {
    blockers.push(
      formBlocker(
        'missing_required_fields',
        'bot',
        'Deployment details incomplete',
        `Missing: ${input.missingRequiredFields.join(', ')}.`,
        confirmInForm('Complete the form', 'ticket-identity'),
      ),
    );
  }

  if (input.deploymentNameError) {
    blockers.push(
      formBlocker(
        'deployment_name_invalid',
        'bot',
        'Deployment name is invalid',
        input.deploymentNameError,
        confirmInForm('Fix deployment name', 'ticket-identity'),
      ),
    );
  }

  if (input.identityConflictSummary !== null) {
    blockers.push(
      formBlocker(
        'identity_coherence_unconfirmed',
        'bot',
        'Run identity needs confirmation',
        input.identityConflictSummary,
        confirmInForm('Confirm identity', 'identity-coherence-card'),
      ),
    );
  }

  if (input.exposureConflictSummary !== null) {
    blockers.push(
      formBlocker(
        'exposure_coherence_unconfirmed',
        'account',
        'Exposure needs confirmation',
        input.exposureConflictSummary,
        confirmInForm('Confirm exposure', 'exposure-launch-decision'),
      ),
    );
  }

  if (input.customSizingError !== null) {
    blockers.push(
      formBlocker(
        'sizing_invalid',
        'bot',
        'Sizing is invalid',
        input.customSizingError,
        confirmInForm('Fix sizing', 'sizing-section'),
      ),
    );
  }

  if (input.dailyOrderLimitError) {
    blockers.push(
      formBlocker(
        'daily_order_limit_invalid',
        'bot',
        'Daily order limit is invalid',
        input.dailyOrderLimitError,
        confirmInForm('Fix daily order limit', 'ticket-launch-settings'),
      ),
    );
  }

  if (input.allInCoexistenceBlock) {
    blockers.push(
      formBlocker(
        'reference_parity_coexistence_blocked',
        'strategy',
        'Reference parity sizing is blocked',
        input.allInCoexistenceBlock,
        confirmInForm('Pick a different sizing preset', 'ticket-sizing'),
      ),
    );
  }

  if (input.liveExecutionSelected) {
    blockers.push(
      formBlocker(
        'live_execution_unavailable',
        'broker',
        'Live execution is unavailable from Deploy',
        'Pick read-only observation or paper orders.',
        confirmInForm('Pick paper or read-only', 'ticket-launch-settings'),
      ),
    );
  }

  if (!input.actionPlanReady) {
    blockers.push(
      formBlocker(
        'action_plan_incomplete',
        'strategy',
        'Entry/exit legs incomplete',
        input.actionPlanMessage ?? 'Add a valid entry leg and a matching close leg before deploying.',
        confirmInForm('Fix the legs', 'ticket-legs'),
      ),
    );
  }

  return blockers;
}

export function deployReady(blockers: OperatorBlocker[]): boolean {
  return blockers.every((b) => b.condition.severity !== 'blocking');
}

export interface MoveDispatch {
  navigate(route: string, fragment: string | null): void;
  focusAnchor(anchor: string): void;
}

export interface RenderedMove {
  label: string;
  variant: 'primary' | 'link';
  invoke(): void;
}

export function resolveBlockerMove(move: OperatorMove, deps: MoveDispatch): RenderedMove | null {
  switch (move.action.kind) {
    case 'navigate': {
      const { route, fragment } = move.action;
      return {
        label: move.label,
        variant: 'link',
        invoke: () => deps.navigate(route, fragment),
      };
    }
    case 'confirm_in_form': {
      const { anchor } = move.action;
      return {
        label: move.label,
        variant: 'primary',
        invoke: () => deps.focusAnchor(anchor),
      };
    }
    default:
      return null;
  }
}
