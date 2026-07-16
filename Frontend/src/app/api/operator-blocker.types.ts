// TS mirror of PythonDataService/app/schemas/operator_blocker.py. Backend
// authors operator prose; the frontend renders headline/detail/labels verbatim.
export type Disposition = 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';
export type OperatorHost =
  | 'bot_cockpit'
  | 'deploy_preflight'
  | 'fleet_roster'
  | 'account_monitor'
  | 'account_desk';
export type OperatorConditionScope = 'bot' | 'account' | 'broker' | 'fleet' | 'host' | 'strategy';
export const OPERATOR_BLOCKER_ANCHOR_KINDS = [
  'surface',
  'verdict',
  'lease',
  'clerk',
  'reconciliation',
  'holdings_row',
  'event',
  'cure_tools',
] as const;
export type OperatorBlockerAnchorKind = (typeof OPERATOR_BLOCKER_ANCHOR_KINDS)[number];
export type OperatorBlockerAudience = 'trader' | 'operator' | 'both';
export type AccountDeskLens = 'trader' | 'operator';

const SUBJECT_KEY_ANCHOR_KINDS: ReadonlySet<OperatorBlockerAnchorKind> = new Set([
  'holdings_row',
  'event',
]);

export interface OperatorBlockerAnchor {
  kind: OperatorBlockerAnchorKind;
  /** Opaque routing token; never render or normalize it as display copy. */
  subject_key: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isOperatorBlockerAnchorKind(value: unknown): value is OperatorBlockerAnchorKind {
  return typeof value === 'string' && OPERATOR_BLOCKER_ANCHOR_KINDS.some((kind) => kind === value);
}

/**
 * Validates a wire anchor for a desk host. Future anchor kinds deliberately
 * collapse to the verdict card so a newer backend cannot make a blocker vanish
 * for an older client.
 */
export function accountDeskAnchorOrVerdictFallback(value: unknown): OperatorBlockerAnchor | null {
  if (!isRecord(value)) return null;

  const kind = value['kind'];
  const subjectKey = value['subject_key'];
  if (typeof kind !== 'string' || (typeof subjectKey !== 'string' && subjectKey !== null)) {
    return null;
  }
  if (!isOperatorBlockerAnchorKind(kind)) return { kind: 'verdict', subject_key: null };
  if (SUBJECT_KEY_ANCHOR_KINDS.has(kind)) {
    return typeof subjectKey === 'string' && subjectKey.length > 0
      ? { kind, subject_key: subjectKey }
      : null;
  }
  return subjectKey === null ? { kind, subject_key: null } : null;
}

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
  confirmation?: OperatorConfirmationCopy | null;
}

export interface OperatorConfirmationCopy {
  title: string;
  body: string;
  consequence: string;
  confirm_label: string;
  required_token: string;
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
  anchor: OperatorBlockerAnchor;
  /** Presentational routing only; never use it as an authorization decision. */
  audience: OperatorBlockerAudience;
  disposition: Disposition;
  headline: string;
  detail: string | null;
  primary_move: OperatorMove | null;
  secondary_moves: OperatorMove[];
  applies_to: 'deploy' | 'run' | 'both';
}

/** Returns the projections whose full backend-authored guidance belongs in a lens. */
export function operatorBlockersForAccountDeskLens(
  blockers: readonly OperatorBlocker[],
  lens: AccountDeskLens,
): readonly OperatorBlocker[] {
  return blockers.filter((blocker) => blocker.audience === lens || blocker.audience === 'both');
}

/**
 * Counts operator-only conditions for the trader's neutral attention summary.
 * Multiple host projections of one condition remain one item.
 */
export function operatorAttentionConditionCount(blockers: readonly OperatorBlocker[]): number {
  return new Set(
    blockers
      .filter((blocker) => blocker.audience === 'operator')
      .map((blocker) => blocker.condition.id),
  ).size;
}

export interface DeployPreflightResponse {
  ready: boolean;
  blockers: OperatorBlocker[];
}
