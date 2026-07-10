// TS mirror of PythonDataService/app/schemas/operator_blocker.py. Backend
// authors operator prose; the frontend renders headline/detail/labels verbatim.
export type Disposition = 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';
export type OperatorHost = 'bot_cockpit' | 'deploy_preflight' | 'fleet_roster' | 'account_monitor';
export type OperatorConditionScope = 'bot' | 'account' | 'broker' | 'fleet' | 'host' | 'strategy';

export interface NavigateAction {
  kind: 'navigate';
  route: string;
  fragment: string | null;
}

export interface ConfirmInFormAction {
  kind: 'confirm_in_form';
  anchor: string;
}

export interface OpenRunbookAction {
  kind: 'open_runbook';
  slug: string;
}

export interface RetireReplaceAction {
  kind: 'retire_replace';
}

export interface RemoveAction {
  kind: 'remove';
}

export type OperatorAction =
  | NavigateAction
  | ConfirmInFormAction
  | OpenRunbookAction
  | RetireReplaceAction
  | RemoveAction;

export interface OperatorMove {
  label: string;
  action: OperatorAction;
  target: string | null;
}

export type BlockerSeverity = 'blocking' | 'warning';

export interface OperatorCondition {
  id: string;
  severity: BlockerSeverity;
  scope: OperatorConditionScope;
  evidence: Record<string, string | number | boolean | null>;
}

export interface OperatorBlocker {
  condition: OperatorCondition;
  host: OperatorHost;
  disposition: Disposition;
  headline: string;
  detail: string | null;
  primary_move: OperatorMove | null;
  secondary_moves: OperatorMove[];
  applies_to: 'deploy' | 'run' | 'both';
}

export interface DeployPreflightResponse {
  ready: boolean;
  blockers: OperatorBlocker[];
}
