// TS mirror of PythonDataService/app/schemas/operator_blocker.py. Backend
// authors operator prose; the frontend renders headline/detail/labels verbatim.
export type Disposition = 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';

export interface NavigateAction {
  kind: 'navigate';
  route: string;
  fragment: string | null;
}

export interface ConfirmInFormAction {
  kind: 'confirm_in_form';
  anchor: string;
}

// Superset grows in Slice 2.
export type OperatorAction = NavigateAction | ConfirmInFormAction;

export interface OperatorMove {
  label: string;
  action: OperatorAction;
  target: string | null;
}

export type BlockerSeverity = 'blocking' | 'warning';

export interface OperatorBlocker {
  id: string;
  severity: BlockerSeverity;
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
